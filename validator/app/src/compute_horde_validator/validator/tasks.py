import asyncio
import time
from collections import defaultdict
from datetime import timedelta

import billiard.exceptions
import bittensor
import celery.exceptions
import numpy as np
from asgiref.sync import async_to_sync
from bittensor.utils.weight_utils import process_weights_for_netuid
from celery import shared_task
from celery.result import allow_join_result
from celery.utils.log import get_task_logger
from compute_horde.utils import get_validators
from django.conf import settings
from django.db import transaction
from django.utils.timezone import now

from compute_horde_validator.celery import app
from compute_horde_validator.validator.metagraph_client import get_miner_axon_info
from compute_horde_validator.validator.models import OrganicJob, SyntheticJobBatch
from compute_horde_validator.validator.synthetic_jobs.utils import (
    MinerClient,
    create_and_run_sythethic_job_batch,
)

from .miner_driver import run_miner_job
from .models import AdminJobRequest

logger = get_task_logger(__name__)

JOB_WINDOW = 2 * 60 * 60
SYNTHETIC_JOBS_SOFT_LIMIT = 2 * 300  # 2 x job timeout - let validator code not to be "instant"
SYNTHETIC_JOBS_HARD_LIMIT = SYNTHETIC_JOBS_SOFT_LIMIT + 10

SCORING_ALGO_VERSION = 2

WEIGHT_SETTING_TTL = 60
WEIGHT_SETTING_HARD_TTL = 65
WEIGHT_SETTING_ATTEMPTS = 100


@app.task(
    soft_time_limit=SYNTHETIC_JOBS_SOFT_LIMIT,
    time_limit=SYNTHETIC_JOBS_HARD_LIMIT,
)
def _run_synthetic_jobs():
    try:
        # metagraph will be refetched and that's fine, after sleeping
        # for e.g. 30 minutes we should refetch the miner list
        create_and_run_sythethic_job_batch(settings.BITTENSOR_NETUID, settings.BITTENSOR_NETWORK)
    except billiard.exceptions.SoftTimeLimitExceeded:
        logger.info("Running synthetic jobs timed out")


@app.task()
def run_synthetic_jobs():
    if not settings.DEBUG_DONT_STAGGER_VALIDATORS:
        validators = get_validators(
            netuid=settings.BITTENSOR_NETUID, network=settings.BITTENSOR_NETWORK
        )
        my_key = settings.BITTENSOR_WALLET().get_hotkey().ss58_address
        validator_keys = [n.hotkey for n in validators]
        if my_key not in validator_keys:
            raise ValueError(
                "Can't determine proper synthetic job window due to not in top 23 validators"
            )
        my_index = validator_keys.index(my_key)
        window_per_validator = JOB_WINDOW / len(validator_keys)
        my_window_starts_at = window_per_validator * my_index
        logger.info(
            f"Sleeping for {my_window_starts_at:02f}s because I am {my_index} out of {len(validator_keys)}"
        )
        time.sleep(my_window_starts_at)
    _run_synthetic_jobs.apply_async()


@app.task()
def do_set_weights(
    netuid: int,
    uids: list,
    weights: list,
    wait_for_inclusion: bool,
    wait_for_finalization: bool,
    version_key: int,
) -> bool:
    """
    Set weights. To be used in other celery tasks in order to facilitate a timeout,
     since the multiprocessing version of this doesn't work in celery.
    """
    subtensor = bittensor.subtensor(network=settings.BITTENSOR_NETWORK)

    bittensor.turn_console_off()
    return subtensor.set_weights(
        wallet=settings.BITTENSOR_WALLET(),
        netuid=netuid,
        uids=np.int64(uids),
        weights=np.float32(weights),
        version_key=version_key,
        wait_for_inclusion=wait_for_inclusion,
        wait_for_finalization=wait_for_finalization,
    )


@shared_task
def trigger_run_admin_job_request(job_request_id: int):
    async_to_sync(run_admin_job_request)(job_request_id)


async def run_admin_job_request(job_request_id: int):
    job_request: AdminJobRequest = await AdminJobRequest.objects.prefetch_related("miner").aget(
        id=job_request_id
    )
    miner = job_request.miner
    miner_axon_info = await get_miner_axon_info(miner.hotkey)
    job = await OrganicJob.objects.acreate(
        job_uuid=str(job_request.uuid),
        miner=miner,
        miner_address=miner_axon_info.ip,
        miner_address_ip_version=miner_axon_info.ip_type,
        miner_port=miner_axon_info.port,
        job_description="Validator Job from Admin Panel",
    )

    keypair = settings.BITTENSOR_WALLET().get_hotkey()
    miner_client = MinerClient(
        loop=asyncio.get_event_loop(),
        miner_address=miner_axon_info.ip,
        miner_port=miner_axon_info.port,
        miner_hotkey=miner.hotkey,
        my_hotkey=keypair.ss58_address,
        job_uuid=job.job_uuid,
        keypair=keypair,
    )
    await run_miner_job(
        miner_client,
        job,
        job_request,
        total_job_timeout=job_request.timeout,
        wait_timeout=job_request.timeout,
        notify_callback=None,
    )


def sigmoid(x, beta, delta):
    return 1 / (1 + np.exp(beta * (-x + delta)))


def reversed_sigmoid(x, beta, delta):
    return sigmoid(-x, beta=beta, delta=-delta)


def horde_score(benchmarks, alpha=0, beta=0, delta=0):
    """Proportionally scores horde benchmarks allowing increasing significance for chosen features

    By default scores are proportional to horde "strength" - having 10 executors would have the same
    score as separate 10 single executor miners. Subnet owner can control significance of defined features:

    alpha - controls significance of average score, so smaller horde can have higher score if executors are stronger;
            best values are from range [0, 1], with 0 meaning no effect
    beta - controls sigmoid function steepness; sigmoid function is over `-(1 / horde_size)`, so larger hordes can be
           more significant than smaller ones, even if summary strength of a horde is the same;
           best values are from range [0,5] (or more, but higher values does not change sigmoid steepnes much),
           with 0 meaning no effect
    delta - controls where sigmoid function has 0.5 value allowing for better control over effect of beta param;
            best values are from range [0, 1]
    """
    sum_agent = sum(benchmarks)
    inverted_n = 1 / len(benchmarks)
    avg_benchmark = sum_agent * inverted_n
    scaled_inverted_n = reversed_sigmoid(inverted_n, beta=beta, delta=delta)
    scaled_avg_benchmark = avg_benchmark**alpha
    return scaled_avg_benchmark * sum_agent * scaled_inverted_n


@app.task
def set_scores():
    subtensor = bittensor.subtensor(network=settings.BITTENSOR_NETWORK)
    metagraph = subtensor.metagraph(netuid=settings.BITTENSOR_NETUID)
    neurons = metagraph.neurons
    hotkey_to_uid = {n.hotkey: n.uid for n in neurons}
    score_per_uid = {}
    batches = list(
        SyntheticJobBatch.objects.prefetch_related("synthetic_jobs").filter(
            scored=False,
            started_at__gte=now() - timedelta(days=1),
            accepting_results_until__lt=now(),
        )
    )
    if not batches:
        logger.info("No batches - nothing to score")
        return

    # scaling factor for avg_score of a horde - best in range [0, 1] (0 means no effect on score)
    alpha = settings.HORDE_SCORE_AVG_PARAM
    # sigmoid steepnes param - best in range [0, 5] (0 means no effect on score)
    beta = settings.HORDE_SCORE_SIZE_PARAM
    # horde size for 0.5 value of sigmoid - sigmoid is for 1 / horde_size
    central_horde_size = settings.HORDE_SCORE_CENTRAL_SIZE_PARAM
    delta = 1 / central_horde_size
    for batch in batches:
        batch_scores = defaultdict(list)
        for job in batch.synthetic_jobs.all():
            uid = hotkey_to_uid.get(job.miner.hotkey)
            if uid is None:
                continue
            batch_scores[uid].append(job.score)
        for uid, uid_batch_scores in batch_scores.items():
            uid_horde_score = horde_score(uid_batch_scores, alpha=alpha, beta=beta, delta=delta)
            score_per_uid[uid] = score_per_uid.get(uid, 0) + uid_horde_score
    if not score_per_uid:
        logger.info("No miners on the subnet to score")
        return
    uids = np.zeros(len(neurons), dtype=np.int64)
    weights = np.zeros(len(neurons), dtype=np.float32)
    for ind, n in enumerate(neurons):
        uids[ind] = n.uid
        weights[ind] = score_per_uid.get(n.uid, 0)

    uids, weights = process_weights_for_netuid(
        uids,
        weights,
        settings.BITTENSOR_NETUID,
        subtensor,
        metagraph,
    )
    with transaction.atomic():
        for batch in batches:
            batch.scored = True
            batch.save()
        for try_number in range(WEIGHT_SETTING_ATTEMPTS):
            logger.debug(f"Setting weights (attempt #{try_number}):\nuids={uids}\nscores={weights}")
            success = False
            message = "unknown error"
            try:
                result = do_set_weights.apply_async(
                    kwargs=dict(
                        netuid=settings.BITTENSOR_NETUID,
                        uids=uids.tolist(),
                        weights=weights.tolist(),
                        wait_for_inclusion=True,
                        wait_for_finalization=False,
                        version_key=SCORING_ALGO_VERSION,
                    ),
                    soft_time_limit=WEIGHT_SETTING_TTL,
                    time_limit=WEIGHT_SETTING_HARD_TTL,
                )
                logger.info(f"Setting weights task id: {result.id}")
                try:
                    with allow_join_result():
                        success = result.get(timeout=WEIGHT_SETTING_TTL)
                        message = "bittensor lib error"
                except (celery.exceptions.TimeoutError, billiard.exceptions.TimeLimitExceeded):
                    result.revoke(terminate=True)
                    logger.info(f"Setting weights timed out (attempt #{try_number})")
                    message = "timeout"
            except Exception:
                logger.exception("Encountered when setting weights: ")
            if not success:
                logger.info(f"Failed to set weights due to {message=} (attempt #{try_number})")
            else:
                logger.info(f"Successfully set weights!!! (attempt #{try_number})")
                break
        else:
            raise RuntimeError(f"Failed to set weights after {WEIGHT_SETTING_ATTEMPTS} attempts")

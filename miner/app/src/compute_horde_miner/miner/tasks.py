import datetime

from celery.utils.log import get_task_logger
from django.conf import settings
from django.utils.timezone import now

from compute_horde.utils import get_validators
from compute_horde_miner.celery import app
from compute_horde_miner.miner import quasi_axon
from compute_horde_miner.miner.models import JobReceipt, Validator
from compute_horde_miner.miner.receipt_store.base import Receipt
from compute_horde_miner.miner.receipt_store.current import receipts_store

logger = get_task_logger(__name__)

RECEIPTS_MAX_RETENTION_PERIOD = datetime.timedelta(days=7)


@app.task
def announce_address_and_port():
    quasi_axon.announce_address_and_port()


@app.task
def fetch_validators():
    validators = get_validators(netuid=settings.BITTENSOR_NETUID, network=settings.BITTENSOR_NETWORK)
    validator_keys = {validator.hotkey for validator in validators}
    to_activate = []
    to_deactivate = []
    to_create = []
    for validator in Validator.objects.all():
        if validator.public_key in validator_keys:
            to_activate.append(validator)
            validator.active = True
            validator_keys.remove(validator.public_key)
        else:
            validator.active = False
            to_deactivate.append(validator)
    for key in validator_keys:
        to_create.append(Validator(public_key=key, active=True))

    Validator.objects.bulk_create(to_create)
    Validator.objects.bulk_update(to_activate + to_deactivate, ['active'])
    logger.info(f'Fetched validators. Activated: {len(to_activate)}, deactivated: {len(to_deactivate)}, '
                f'created: {len(to_create)}')


@app.task
def prepare_receipts():
    receipts = [Receipt.from_job_receipt(jr) for jr in JobReceipt.objects.all()]
    receipts_store.store(receipts)


@app.task
def clear_old_receipts():
    JobReceipt.objects.filter(time_started__lt=now()-RECEIPTS_MAX_RETENTION_PERIOD).delete()

from collections.abc import Callable
import uuid

import asyncio
import bittensor
import pytest
import pytest_asyncio
from compute_horde.miner_client.base import AbstractTransport
from compute_horde_validator.validator.synthetic_jobs.batch_run import execute_synthetic_batch_run

from compute_horde_validator.validator.models import Miner
from compute_horde_validator.validator.synthetic_jobs.batch_run import BatchContext, MinerClient
from compute_horde_validator.validator.tests.transport import MinerSimulationTransport


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.django_db(databases=["default", "default_alias"], transaction=True),
]


@pytest.fixture
def num_miners():
    return 5


@pytest.fixture
def job_uuids(num_miners: int):
    return [uuid.uuid4() for _ in range(num_miners)]


@pytest.fixture
def miner_hotkeys(num_miners: int):
    return [f"hotkey_{i}" for i in range(num_miners)]


@pytest_asyncio.fixture
async def miners(miner_hotkeys: list[str]):
    objs = [Miner(hotkey=hotkey) for hotkey in miner_hotkeys]
    await Miner.objects.abulk_create(objs)
    return objs


@pytest.fixture
def miner_axon_infos(miner_hotkeys: str):
    return [
        bittensor.AxonInfo(
            version=4,
            ip="ignore",
            ip_type=4,
            port=9999,
            hotkey=hotkey,
            coldkey=hotkey,
        )
        for hotkey in miner_hotkeys
    ]


@pytest.fixture
def axon_dict(miner_axon_infos: list[bittensor.AxonInfo]):
    return {axon.hotkey: axon for axon in miner_axon_infos}


@pytest_asyncio.fixture
async def transports(miner_hotkeys: str):
    return [MinerSimulationTransport(hotkey) for hotkey in miner_hotkeys]


@pytest.fixture
def create_simulation_miner_client(miner_hotkeys: list[str], transports: list[AbstractTransport]):
    transport_dict = {hotkey: transport for hotkey, transport in zip(miner_hotkeys, transports)}

    def _create(ctx: BatchContext, miner_hotkey: str):
        return MinerClient(
            ctx=ctx, miner_hotkey=miner_hotkey, transport=transport_dict[miner_hotkey]
        )

    return _create


async def test_success(
    axon_dict: dict[str, bittensor.AxonInfo],
    transports: list[MinerSimulationTransport],
    miners: list[Miner],
    create_simulation_miner_client: Callable,
    manifest_message: str,
):
    for transport in transports:
        await transport.add_message(manifest_message, send_before=1)

    await asyncio.wait_for(
        execute_synthetic_batch_run(
            axon_dict,
            miners,
            create_miner_client=create_simulation_miner_client,
        ),
        timeout=1,
    )

import bittensor
from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.management.base import BaseCommand

from .connect_facilitator import FacilitatorClient


class DebugFacilitatorClient(FacilitatorClient):
    def __init__(
        self,
        keypair: bittensor.Keypair,
        facilitator_uri: str,
        miner_hotkey: str,
        miner_address: str,
        miner_port: int,
    ):
        super().__init__(keypair, facilitator_uri)
        self.miner_hotkey = miner_hotkey
        self.miner_address = miner_address
        self.miner_port = miner_port

    async def get_miner_axon_info(self, hotkey: str) -> bittensor.AxonInfo:
        if hotkey != self.miner_hotkey:
            raise ValueError("unsupported hotkey")
        return bittensor.AxonInfo(
            version=4,
            ip=self.miner_address,
            ip_type=4,
            port=self.miner_port,
            hotkey=self.miner_hotkey,
            coldkey=self.miner_hotkey,  # I hope it does not matter
        )

    def create_metagraph_refresh_task(self, period=None):
        return None


class Command(BaseCommand):
    FACILITATOR_CLIENT_CLASS = DebugFacilitatorClient

    def add_arguments(self, parser):
        parser.add_argument("--miner_hotkey", type=str, help="Miner hotkey", required=True)
        parser.add_argument(
            "--miner_address", type=str, help="Miner IPv4 address", default="127.0.0.1"
        )
        parser.add_argument("--miner_port", type=int, help="Miner port", default=8000)

    @async_to_sync
    async def handle(self, *args, **options):
        miner_hotkey = options["miner_hotkey"]
        miner_address = options["miner_address"]
        miner_port = options["miner_port"]
        keypair = settings.BITTENSOR_WALLET().get_hotkey()
        print(settings.FACILITATOR_URI)
        facilitator_client = self.FACILITATOR_CLIENT_CLASS(
            keypair, settings.FACILITATOR_URI, miner_hotkey, miner_address, miner_port
        )
        async with facilitator_client:
            await facilitator_client.run_forever()

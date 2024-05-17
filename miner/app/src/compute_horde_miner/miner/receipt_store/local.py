import pathlib
import shutil
import tempfile
import zipfile

from django.conf import settings

from compute_horde_miner.miner.receipt_store.base import BaseReceiptStore, Receipt

ZIP_FILENAME = "receipts.zip"


class LocalReceiptStore(BaseReceiptStore):
    def store(self, receipts: list[Receipt]) -> None:
        if not receipts:
            return

        root = pathlib.Path(settings.LOCAL_RECEIPTS_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        filepath = root / ZIP_FILENAME

        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            with zipfile.ZipFile(temp_file, mode='w', compression=zipfile.ZIP_LZMA) as zf:
                for receipt in receipts:
                    zf.writestr(receipt.payload.job_uuid + '.json', receipt.json())

        shutil.move(temp_file.name, filepath)
        filepath.chmod(0o644)

    def get_url(self) -> str:
        return settings.LOCAL_RECEIPTS_URL + ZIP_FILENAME

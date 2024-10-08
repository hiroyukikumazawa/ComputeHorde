import asyncio
import base64
import csv
import io
import logging
import pathlib
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import zipfile

import httpx
import pydantic
from compute_horde.base.volume import (
    InlineVolume,
    MultiVolume,
    SingleFileVolume,
    Volume,
    ZipUrlVolume,
)
from compute_horde.base_requests import BaseRequest
from compute_horde.em_protocol import executor_requests, miner_requests
from compute_horde.em_protocol.executor_requests import (
    GenericError,
    V0FailedRequest,
    V0FailedToPrepare,
    V0FinishedRequest,
    V0MachineSpecsRequest,
    V0ReadyRequest,
)
from compute_horde.em_protocol.miner_requests import (
    BaseMinerRequest,
    V0InitialJobRequest,
    V0JobRequest,
)
from compute_horde.miner_client.base import (
    AbstractMinerClient,
    AbstractTransport,
    UnsupportedMessageReceived,
)
from compute_horde.transport import WSTransport
from compute_horde.utils import MachineSpecs
from django.conf import settings
from django.core.management.base import BaseCommand

from compute_horde_executor.executor.output_uploader import OutputUploader, OutputUploadFailed

logger = logging.getLogger(__name__)

CVE_2022_0492_TIMEOUT_SECONDS = 120
MAX_RESULT_SIZE_IN_RESPONSE = 1000
TRUNCATED_RESPONSE_PREFIX_LEN = 100
TRUNCATED_RESPONSE_SUFFIX_LEN = 100
INPUT_VOLUME_UNPACK_TIMEOUT_SECONDS = 300
CVE_2022_0492_IMAGE = (
    "us-central1-docker.pkg.dev/twistlock-secresearch/public/can-ctr-escape-cve-2022-0492:latest"
)


class RunConfigManager:
    @classmethod
    def preset_to_docker_run_args(cls, preset: str) -> list[str]:
        if preset == "none":
            return []
        elif preset == "nvidia_all":
            return ["--runtime=nvidia", "--gpus", "all"]
        else:
            raise JobError(f"Invalid preset: {preset}")

    @classmethod
    def preset_to_image_for_raw_script(cls, preset: str) -> str:
        # TODO: return pre-built base image for preset, i.e. with numpy, pandas, tensorflow, torch etc.
        return "python:3.11-slim"


class MinerClient(AbstractMinerClient):
    def __init__(self, miner_address: str, token: str, transport: AbstractTransport | None = None):
        self.miner_address = miner_address
        self.token = token
        transport = transport or WSTransport(miner_address, self.miner_url())
        super().__init__(miner_address, transport)

        self.job_uuid: str | None = None
        loop = asyncio.get_running_loop()
        self.initial_msg = loop.create_future()
        self.initial_msg_lock = asyncio.Lock()
        self.full_payload = loop.create_future()
        self.full_payload_lock = asyncio.Lock()

    def miner_url(self) -> str:
        return f"{self.miner_address}/v0.1/executor_interface/{self.token}"

    def accepted_request_type(self) -> type[BaseRequest]:
        return BaseMinerRequest

    def incoming_generic_error_class(self):
        return miner_requests.GenericError

    def outgoing_generic_error_class(self):
        return executor_requests.GenericError

    async def handle_message(self, msg: BaseRequest):
        if isinstance(msg, V0InitialJobRequest):
            await self.handle_initial_job_request(msg)
        elif isinstance(msg, V0JobRequest):
            await self.handle_job_request(msg)
        else:
            raise UnsupportedMessageReceived(msg)

    async def handle_initial_job_request(self, msg: V0InitialJobRequest):
        async with self.initial_msg_lock:
            if self.initial_msg.done():
                msg = f"Received duplicate initial job request: first {self.job_uuid=} and then {msg.job_uuid=}"
                logger.error(msg)
                self.deferred_send_model(GenericError(details=msg))
                return
            self.job_uuid = msg.job_uuid
            logger.debug(f"Received initial job request: {msg.job_uuid=}")
            self.initial_msg.set_result(msg)

    async def handle_job_request(self, msg: V0JobRequest):
        async with self.full_payload_lock:
            if not self.initial_msg.done():
                msg = f"Received job request before an initial job request {msg.job_uuid=}"
                logger.error(msg)
                await self.deferred_send_model(GenericError(details=msg))
                return
            if self.full_payload.done():
                msg = (
                    f"Received duplicate full job payload request: first "
                    f"{self.job_uuid=} and then {msg.job_uuid=}"
                )
                logger.error(msg)
                await self.deferred_send_model(GenericError(details=msg))
                return
            logger.debug(f"Received full job payload request: {msg.job_uuid=}")
            self.full_payload.set_result(msg)

    async def send_ready(self):
        await self.send_model(V0ReadyRequest(job_uuid=self.job_uuid))

    async def send_finished(self, job_result: "JobResult"):
        if job_result.specs:
            await self.send_model(
                V0MachineSpecsRequest(
                    job_uuid=self.job_uuid,
                    specs=job_result.specs,
                )
            )
        await self.send_model(
            V0FinishedRequest(
                job_uuid=self.job_uuid,
                docker_process_stdout=job_result.stdout,
                docker_process_stderr=job_result.stderr,
            )
        )

    async def send_failed(self, job_result: "JobResult"):
        await self.send_model(
            V0FailedRequest(
                job_uuid=self.job_uuid,
                docker_process_exit_status=job_result.exit_status,
                timeout=job_result.timeout,
                docker_process_stdout=job_result.stdout,
                docker_process_stderr=job_result.stderr,
            )
        )

    async def send_generic_error(self, details: str):
        await self.send_model(
            GenericError(
                details=details,
            )
        )

    async def send_failed_to_prepare(self):
        await self.send_model(
            V0FailedToPrepare(
                job_uuid=self.job_uuid,
            )
        )


class JobResult(pydantic.BaseModel):
    success: bool
    exit_status: int | None
    timeout: bool
    stdout: str
    stderr: str
    specs: MachineSpecs | None = None


def truncate(v: str) -> str:
    if len(v) > MAX_RESULT_SIZE_IN_RESPONSE:
        return f"{v[:TRUNCATED_RESPONSE_PREFIX_LEN]} ... {v[-TRUNCATED_RESPONSE_SUFFIX_LEN:]}"
    else:
        return v


def run_cmd(cmd):
    proc = subprocess.run(cmd, shell=True, capture_output=True, check=False, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"run_cmd error {cmd=!r} {proc.returncode=} {proc.stdout=!r} {proc.stderr=!r}"
        )
    return proc.stdout


def get_machine_specs() -> MachineSpecs:
    data = {}

    data["gpu"] = {"count": 0, "details": []}
    try:
        nvidia_cmd = run_cmd(
            "docker run --rm --runtime=nvidia --gpus all ubuntu "
            "nvidia-smi --query-gpu=name,driver_version,name,memory.total,compute_cap,power.limit,clocks.gr,clocks.mem,uuid,serial --format=csv"
        )
        csv_data = csv.reader(nvidia_cmd.splitlines())
        header = [x.strip() for x in next(csv_data)]
        for row in csv_data:
            row = [x.strip() for x in row]
            gpu_data = dict(zip(header, row))
            data["gpu"]["details"].append(
                {
                    "name": gpu_data["name"],
                    "driver": gpu_data["driver_version"],
                    "capacity": gpu_data["memory.total [MiB]"].split(" ")[0],
                    "cuda": gpu_data["compute_cap"],
                    "power_limit": gpu_data["power.limit [W]"].split(" ")[0],
                    "graphics_speed": gpu_data["clocks.current.graphics [MHz]"].split(" ")[0],
                    "memory_speed": gpu_data["clocks.current.memory [MHz]"].split(" ")[0],
                    "uuid": gpu_data["uuid"].split(" ")[0],
                    "serial": gpu_data["serial"].split(" ")[0],
                }
            )
        data["gpu"]["count"] = len(data["gpu"]["details"])
    except Exception as exc:
        # print(f'Error processing scraped gpu specs: {exc}', flush=True)
        data["gpu_scrape_error"] = repr(exc)

    data["cpu"] = {"count": 0, "model": "", "clocks": []}
    try:
        lscpu_output = run_cmd("lscpu")
        data["cpu"]["model"] = re.search(r"Model name:\s*(.*)$", lscpu_output, re.M).group(1)
        data["cpu"]["count"] = int(re.search(r"CPU\(s\):\s*(.*)", lscpu_output).group(1))

        cpu_data = run_cmd('lscpu --parse=MHZ | grep -Po "^[0-9,.]*$"').splitlines()
        data["cpu"]["clocks"] = [float(x) for x in cpu_data]
    except Exception as exc:
        # print(f'Error getting cpu specs: {exc}', flush=True)
        data["cpu_scrape_error"] = repr(exc)

    data["ram"] = {}
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()

        for name, key in [
            ("MemAvailable", "available"),
            ("MemFree", "free"),
            ("MemTotal", "total"),
        ]:
            data["ram"][key] = int(re.search(rf"^{name}:\s*(\d+)\s+kB$", meminfo, re.M).group(1))
        data["ram"]["used"] = data["ram"]["total"] - data["ram"]["free"]
    except Exception as exc:
        # print(f"Error reading /proc/meminfo; Exc: {exc}", file=sys.stderr)
        data["ram_scrape_error"] = repr(exc)

    data["hard_disk"] = {}
    try:
        disk_usage = shutil.disk_usage(".")
        data["hard_disk"] = {
            "total": disk_usage.total // 1024,  # in kiB
            "used": disk_usage.used // 1024,
            "free": disk_usage.free // 1024,
        }
    except Exception as exc:
        # print(f"Error getting disk_usage from shutil: {exc}", file=sys.stderr)
        data["hard_disk_scrape_error"] = repr(exc)

    data["os"] = ""
    try:
        data["os"] = run_cmd('lsb_release -d | grep -Po "Description:\\s*\\K.*"').strip()
    except Exception as exc:
        # print(f'Error getting os specs: {exc}', flush=True)
        data["os_scrape_error"] = repr(exc)

    return MachineSpecs(specs=data)


class JobError(Exception):
    def __init__(self, description: str):
        self.description = description


class DownloadManager:
    def __init__(self, concurrency=3, max_retries=3):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries

    async def download(self, fp, url):
        async with self.semaphore:
            retries = 0
            bytes_received = 0
            backoff_factor = 1

            while retries < self.max_retries:
                headers = {}
                if bytes_received > 0:
                    headers["Range"] = f"bytes={bytes_received}-"

                async with httpx.AsyncClient() as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code == 416:  # Requested Range Not Satisfiable
                            # Server doesn't support resume, start from the beginning
                            fp.seek(0)
                            bytes_received = 0
                            continue
                        elif response.status_code != 206 and bytes_received > 0:  # Partial Content
                            # Server doesn't support resume, start from the beginning
                            fp.seek(0)
                            bytes_received = 0
                            continue

                        if (
                            bytes_received == 0
                            and (content_length := response.headers.get("Content-Length"))
                            is not None
                        ):
                            # check size early if Content-Length is present
                            if 0 < settings.VOLUME_MAX_SIZE_BYTES < int(content_length):
                                raise JobError("Input volume too large")

                        try:
                            async for chunk in response.aiter_bytes():
                                bytes_received += len(chunk)
                                if 0 < settings.VOLUME_MAX_SIZE_BYTES < bytes_received:
                                    raise JobError("Input volume too large")
                                fp.write(chunk)
                            return  # Download completed successfully
                        except (httpx.HTTPError, OSError) as e:
                            retries += 1
                            if retries >= self.max_retries:
                                raise e

                            # Exponential backoff with jitter
                            backoff_time = backoff_factor * (2 ** (retries - 1))
                            jitter = random.uniform(
                                0, 0.1
                            )  # Add jitter to avoid synchronization issues
                            backoff_time *= 1 + jitter
                            await asyncio.sleep(backoff_time)

                            backoff_factor *= 2  # Double the backoff factor for the next retry

            raise JobError(f"Download failed after {self.max_retries} retries")


class JobRunner:
    def __init__(self, initial_job_request: V0InitialJobRequest):
        self.initial_job_request = initial_job_request
        self.full_job_request: None | V0JobRequest = None
        self.temp_dir = pathlib.Path(tempfile.mkdtemp())
        self.volume_mount_dir = self.temp_dir / "volume"
        self.output_volume_mount_dir = self.temp_dir / "output"
        self.specs_volume_mount_dir = self.temp_dir / "specs"
        self.download_manager = DownloadManager()

    async def prepare(self):
        self.volume_mount_dir.mkdir(exist_ok=True)
        self.output_volume_mount_dir.mkdir(exist_ok=True)

        if self.initial_job_request.base_docker_image_name is not None:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "pull",
                self.initial_job_request.base_docker_image_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                msg = (
                    f'"docker pull {self.initial_job_request.base_docker_image_name}" '
                    f"(job_uuid={self.initial_job_request.job_uuid})"
                    f" failed with status={process.returncode}"
                    f' stdout="{stdout.decode()}"\nstderr="{stderr.decode()}'
                )
                logger.error(msg)
                raise JobError(msg)

    async def run_job(self, job_request: V0JobRequest):
        self.full_job_request = job_request
        try:
            docker_run_options = RunConfigManager.preset_to_docker_run_args(
                job_request.docker_run_options_preset
            )
            await self.unpack_volume()
        except JobError as ex:
            logger.error("Job error: %s", ex.description)
            return JobResult(
                success=False,
                exit_status=None,
                timeout=False,
                stdout=ex.description,
                stderr="",
            )

        docker_image = job_request.docker_image_name
        extra_volume_flags = []
        docker_run_cmd = job_request.docker_run_cmd

        if job_request.raw_script:
            if docker_image is None:
                docker_image = RunConfigManager.preset_to_image_for_raw_script(
                    job_request.docker_run_options_preset
                )
            raw_script_path = self.temp_dir / "script.py"
            raw_script_path.write_text(job_request.raw_script)
            extra_volume_flags = ["-v", f"{raw_script_path.absolute().as_posix()}:/script.py"]

            if not docker_run_cmd:
                docker_run_cmd = ["python", "/script.py"]

        cmd = [
            "docker",
            "run",
            *docker_run_options,
            "--name",
            f"{settings.EXECUTOR_TOKEN}-job",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{self.volume_mount_dir.as_posix()}/:/volume/",
            "-v",
            f"{self.output_volume_mount_dir.as_posix()}/:/output/",
            "-v",
            f"{self.specs_volume_mount_dir.as_posix()}/:/specs/",
            *extra_volume_flags,
            docker_image,
            *docker_run_cmd,
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        t1 = time.time()
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.initial_job_request.timeout_seconds
            )

        except TimeoutError:
            # If the process did not finish in time, kill it
            logger.error(
                f"Process didn't finish in time, killing it, job_uuid={self.initial_job_request.job_uuid}"
            )
            process.kill()
            timeout = True
            exit_status = None
            stdout = (await process.stdout.read()).decode()
            stderr = (await process.stderr.read()).decode()
        else:
            stdout = stdout.decode()
            stderr = stderr.decode()
            exit_status = process.returncode
            timeout = False

        # Save the streams in output volume and truncate them in response.
        with open(self.output_volume_mount_dir / "stdout.txt", "w") as f:
            f.write(stdout)
        stdout = truncate(stdout)
        with open(self.output_volume_mount_dir / "stderr.txt", "w") as f:
            f.write(stderr)
        stderr = truncate(stderr)

        success = exit_status == 0

        if success:
            # upload the output if requested and job succeeded
            if job_request.output_upload:
                try:
                    output_uploader = OutputUploader.for_upload_output(job_request.output_upload)
                    await output_uploader.upload(self.output_volume_mount_dir)
                except OutputUploadFailed as ex:
                    logger.warning(
                        f"Uploading output failed for job {self.initial_job_request.job_uuid} with error: {ex!r}"
                    )
                    success = False
                    stdout = ex.description
                    stderr = ""

            time_took = time.time() - t1
            logger.info(
                f'Job "{self.initial_job_request.job_uuid}" finished successfully in {time_took:0.2f} seconds'
            )
        else:
            time_took = time.time() - t1
            logger.error(
                f'"{" ".join(cmd)}" (job_uuid={self.initial_job_request.job_uuid})'
                f' failed after {time_took:0.2f} seconds with status={process.returncode}'
                f' \nstdout="{stdout}"\nstderr="{stderr}'
            )

        return JobResult(
            success=success,
            exit_status=exit_status,
            timeout=timeout,
            stdout=stdout,
            stderr=stderr,
        )

    async def clean(self):
        # remove input/output directories with docker, to deal with funky file permissions
        root_for_remove = pathlib.Path("/temp_dir/")
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "-v",
            f"{self.temp_dir.as_posix()}/:/{root_for_remove.as_posix()}/",
            "alpine:3.19",
            "sh",
            "-c",
            f"rm -rf {shlex.quote(root_for_remove.as_posix())}/*",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
        self.temp_dir.rmdir()

    async def _unpack_volume(self, volume: Volume | None):
        assert str(self.volume_mount_dir) not in {"~", "/"}
        for path in self.volume_mount_dir.glob("*"):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

        if volume is not None:
            if isinstance(volume, InlineVolume):
                await self._unpack_inline_volume(volume)
            elif isinstance(volume, ZipUrlVolume):
                await self._unpack_zip_url_volume(volume)
            elif isinstance(volume, SingleFileVolume):
                await self._unpack_single_file_volume(volume)
            elif isinstance(volume, MultiVolume):
                await self._unpack_multi_volume(volume)
            else:
                raise NotImplementedError(f"Unsupported volume_type: {volume.volume_type}")

        chmod_proc = await asyncio.create_subprocess_exec(
            "chmod", "-R", "777", self.temp_dir.as_posix()
        )
        assert 0 == await chmod_proc.wait()

    async def _unpack_inline_volume(self, volume: InlineVolume):
        decoded_contents = base64.b64decode(volume.contents)
        bytes_io = io.BytesIO(decoded_contents)
        zip_file = zipfile.ZipFile(bytes_io)
        extraction_path = self.volume_mount_dir
        if volume.relative_path:
            extraction_path /= volume.relative_path
        zip_file.extractall(extraction_path.as_posix())

    async def _unpack_zip_url_volume(self, volume: ZipUrlVolume):
        with tempfile.NamedTemporaryFile() as download_file:
            await self.download_manager.download(download_file, volume.contents)
            download_file.seek(0)
            zip_file = zipfile.ZipFile(download_file)
            extraction_path = self.volume_mount_dir
            if volume.relative_path:
                extraction_path /= volume.relative_path
            zip_file.extractall(extraction_path.as_posix())

    async def _unpack_single_file_volume(self, volume: SingleFileVolume):
        file_path = self.volume_mount_dir / volume.relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("wb") as file:
            await self.download_manager.download(file, volume.url)

    async def _unpack_multi_volume(self, volume: MultiVolume):
        for sub_volume in volume.volumes:
            if isinstance(sub_volume, InlineVolume):
                await self._unpack_inline_volume(sub_volume)
            elif isinstance(sub_volume, ZipUrlVolume):
                await self._unpack_zip_url_volume(sub_volume)
            elif isinstance(sub_volume, SingleFileVolume):
                await self._unpack_single_file_volume(sub_volume)
            else:
                raise NotImplementedError(f"Unsupported sub-volume type: {type(sub_volume)}")

    async def get_job_volume(self) -> Volume | None:
        if self.full_job_request.volume and self.initial_job_request.volume:
            raise JobError("Received multiple volumes")

        return self.full_job_request.volume or self.initial_job_request.volume or None

    async def unpack_volume(self):
        try:
            await asyncio.wait_for(
                self._unpack_volume(await self.get_job_volume()),
                timeout=INPUT_VOLUME_UNPACK_TIMEOUT_SECONDS,
            )
        except JobError:
            raise
        except TimeoutError as exc:
            raise JobError("Input volume downloading took too long") from exc
        except Exception as exc:
            logger.exception("error occurred during unpacking input volume")
            raise JobError("Unknown error happened while downloading input volume") from exc


class Command(BaseCommand):
    help = "Run the executor, query the miner for job details, and run the job docker"

    MINER_CLIENT_CLASS = MinerClient
    JOB_RUNNER_CLASS = JobRunner

    def handle(self, *args, **options):
        self.miner_client_for_tests = asyncio.run(self._executor_loop())

    async def is_system_safe_for_cve_2022_0492(self):
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            CVE_2022_0492_IMAGE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), CVE_2022_0492_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.error("CVE-2022-0492 check timed out")
            return False

        if process.returncode != 0:
            logger.error(
                f'CVE-2022-0492 check failed: stdout="{stdout.decode()}"\nstderr="{stderr.decode()}'
            )
            return False
        expected_output = "Contained: cannot escape via CVE-2022-0492"
        if expected_output not in stdout.decode():
            logger.error(
                f'CVE-2022-0492 check failed: "{expected_output}" not in stdout.'
                f'stdout="{stdout.decode()}"\nstderr="{stderr.decode()}'
            )
            return False

        return True

    async def _executor_loop(self):
        logger.debug(f"Connecting to miner: {settings.MINER_ADDRESS}")
        miner_client = self.MINER_CLIENT_CLASS(settings.MINER_ADDRESS, settings.EXECUTOR_TOKEN)
        async with miner_client:
            logger.debug(f"Connected to miner: {settings.MINER_ADDRESS}")
            initial_message: V0InitialJobRequest = await miner_client.initial_msg
            if initial_message.base_docker_image_name and not (
                initial_message.base_docker_image_name.startswith("backenddevelopersltd/")
                or initial_message.base_docker_image_name.startswith(
                    "docker.io/backenddevelopersltd/"
                )
            ):
                await miner_client.send_failed_to_prepare()
                return
            logger.debug("Checking for CVE-2022-0492 vulnerability")
            if not await self.is_system_safe_for_cve_2022_0492():
                await miner_client.send_failed_to_prepare()
                return

            job_runner = self.JOB_RUNNER_CLASS(initial_message)
            try:
                logger.debug(f"Preparing for job {initial_message.job_uuid}")
                try:
                    await job_runner.prepare()
                except JobError:
                    await miner_client.send_failed_to_prepare()
                    return

                logger.debug(f"Scraping hardware specs for job {initial_message.job_uuid}")
                specs = get_machine_specs()

                await miner_client.send_ready()
                logger.debug(f"Informed miner that I'm ready for job {initial_message.job_uuid}")

                job_request = await miner_client.full_payload
                if job_request.docker_image_name and not (
                    job_request.docker_image_name.startswith("backenddevelopersltd/")
                    or job_request.docker_image_name.startswith("docker.io/backenddevelopersltd/")
                ):
                    await miner_client.send_failed_to_prepare()
                    return
                logger.debug(f"Running job {initial_message.job_uuid}")
                result = await job_runner.run_job(job_request)
                result.specs = specs

                if result.success:
                    await miner_client.send_finished(result)
                else:
                    await miner_client.send_failed(result)
            except Exception:
                logger.error(
                    f"Unhandled exception when working on job {initial_message.job_uuid}",
                    exc_info=True,
                )
                # not deferred, because this is the end of the process, making it deferred would cause it never
                # to be sent
                await miner_client.send_generic_error("Unexpected error")
            finally:
                await job_runner.clean()

        return miner_client

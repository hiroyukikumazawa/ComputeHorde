import asyncio
import time

import constance.utils
from asgiref.sync import sync_to_async
from django.conf import settings


class DynamicConfigHolder:
    CACHE_TIMEOUT = 300

    def __init__(self):
        self._time_set = 0
        self._lock = asyncio.Lock()
        self._config = {k: v[0] for k, v in settings.CONSTANCE_CONFIG.items()}

    async def get(self, key):
        async with self._lock:
            if time.time() - self._time_set > self.CACHE_TIMEOUT:
                self._config = await sync_to_async(constance.utils.get_values)()
                self._time_set = time.time()

        return self._config[key]


dynamic_config_holder = DynamicConfigHolder()


async def aget_config(key):
    return await dynamic_config_holder.get(key)


async def aget_weights_version():
    if settings.DEBUG_OVERRIDE_WEIGHTS_VERSION is not None:
        return settings.DEBUG_OVERRIDE_WEIGHTS_VERSION
    return await aget_config("DYNAMIC_WEIGHTS_VERSION")


# this is called from a sync context, and rarely, so we don't need caching
def get_synthetic_jobs_flow_version() -> int:
    config = constance.utils.get_values()
    value = config.get("DYNAMIC_SYNTHETIC_JOBS_FLOW_VERSION")
    if value is not None:
        return value
    return settings.SYNTHETIC_JOBS_FLOW_VERSION

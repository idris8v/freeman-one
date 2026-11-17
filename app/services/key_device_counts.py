"""Read authoritative HWID counts with bounded concurrency and no false zero fallback."""

import asyncio


async def device_counts(requests):
    gate = asyncio.Semaphore(4)

    async def read(key_id, api, uuid):
        try:
            async with gate:
                from app.services.device_inventory import devices
                items = await devices(api,uuid)
            from app.services.vless_hwid import parse_devices

            return key_id, len(items)
        except Exception:
            return key_id, None

    return dict(await asyncio.gather(*(read(*request) for request in requests)))

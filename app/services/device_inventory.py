"""Short, bounded HWID reads shared by cards and device navigation."""
import asyncio,hashlib,time
from copy import deepcopy
from collections import OrderedDict

TTL=15
TIMEOUT=2.5
_cache=OrderedDict()
_pending={}
_gate=asyncio.Semaphore(8)

def key(api,remote):
    identity=str(getattr(api,'base_url',id(api)))+'|'+str(getattr(api,'token',''))+'|'+str(getattr(api,'gateway_token',''))
    return hashlib.sha256(identity.encode()).hexdigest(),str(remote)

def invalidate(remote):
    for k in list(_cache):
        if k[1]==str(remote):_cache.pop(k,None)
    for k in list(_pending):
        if k[1]==str(remote):_pending.pop(k,None)

async def devices(api,remote,refresh=False):
    k=key(api,remote)
    if refresh:invalidate(remote)
    cached=_cache.get(k)
    if cached and cached[0]>time.monotonic():
        _cache.move_to_end(k)
        return deepcopy(cached[1])
    async def load():
        from app.services.vless_hwid import parse_devices
        async def read():
            async with _gate:return await api.devices(remote)
        payload=await asyncio.wait_for(read(),TIMEOUT)
        items=parse_devices(payload,remote)
        if _pending.get(k) is asyncio.current_task():
            _cache[k]=(time.monotonic()+TTL,deepcopy(items));_cache.move_to_end(k)
            while len(_cache)>256:_cache.popitem(last=False)
        return items
    task=_pending.get(k)
    if task is None:
        task=asyncio.create_task(load());_pending[k]=task
        def done(t):
            if _pending.get(k) is t:_pending.pop(k,None)
            if not t.cancelled():t.exception()
        task.add_done_callback(done)
    return deepcopy(await asyncio.shield(task))

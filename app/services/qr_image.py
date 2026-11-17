"""Render QR images outside the event loop; cache bounded immutable PNG bytes."""
import asyncio,hashlib
from collections import OrderedDict
from weakref import WeakKeyDictionary

_cache=OrderedDict()
_loops=WeakKeyDictionary()

def generate(value):
    import io,qrcode
    output=io.BytesIO();qrcode.make(value).save(output,format='PNG')
    return output.getvalue()

async def png(value):
    key=hashlib.sha256(value.encode()).digest()
    if key in _cache:
        _cache.move_to_end(key);return _cache[key]
    loop=asyncio.get_running_loop()
    if loop not in _loops:_loops[loop]=(asyncio.Semaphore(2),{})
    gate,pending=_loops[loop]
    async def load():
        async with gate:result=await asyncio.to_thread(generate,value)
        _cache[key]=result;_cache.move_to_end(key)
        while len(_cache)>128:_cache.popitem(last=False)
        return result
    task=pending.get(key)
    if task is None:
        task=asyncio.create_task(load());pending[key]=task
        def done(t):
            if pending.get(key) is t:pending.pop(key,None)
            if not t.cancelled():t.exception()
        task.add_done_callback(done)
    return await asyncio.shield(task)

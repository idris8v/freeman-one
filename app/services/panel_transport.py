"""Bounded, credential-independent connection pool for configured panel APIs."""
import asyncio
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary
import aiohttp

_sessions=WeakKeyDictionary()

@asynccontextmanager
async def session():
    loop=asyncio.get_running_loop();client=_sessions.get(loop)
    if client is None or client.closed:
        client=aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=16,limit_per_host=8,ttl_dns_cache=300,keepalive_timeout=30),timeout=aiohttp.ClientTimeout(total=15),cookie_jar=aiohttp.DummyCookieJar())
        _sessions[loop]=client
    yield client

async def close():
    client=_sessions.pop(asyncio.get_running_loop(),None)
    if client is not None:await client.close()

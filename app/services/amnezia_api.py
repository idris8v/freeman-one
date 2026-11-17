import asyncio
import aiohttp


class AmneziaApiError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class AmneziaApiClient:
    def __init__(self, base_url, api_key, timeout=12):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout, connect=5)

    async def request(self, method, path, *, json=None, auth=True):
        headers = {'x-api-key': self.api_key} if auth else {}
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.request(method, self.base_url + path, json=json, headers=headers) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = {'message': (await response.text())[:300]}
                    if response.status >= 400:
                        raise AmneziaApiError(str(payload.get('message') or f'HTTP {response.status}')[:300], response.status)
                    return payload
        except AmneziaApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise AmneziaApiError(f'API недоступен: {type(error).__name__}') from error

    async def health(self): return await self.request('GET', '/healthz', auth=False)
    async def server(self): return await self.request('GET', '/server')
    async def load(self): return await self.request('GET', '/server/load')
    async def clients(self, skip=0, limit=100): return await self.request('GET', f'/clients?skip={int(skip)}&limit={int(limit)}')
    async def create_client(self, name, protocol, expires_at):
        return await self.request('POST', '/clients', json={'clientName': name, 'protocol': protocol, 'expiresAt': expires_at})
    async def qr(self, config): return await self.request('POST', '/clients/qr', json={'config': config})
    async def update_client(self, client_id, protocol, **values):
        return await self.request('PATCH', '/clients', json={'clientId': client_id, 'protocol': protocol, **values})
    async def delete_client(self, client_id, protocol):
        return await self.request('DELETE', '/clients', json={'clientId': client_id, 'protocol': protocol})

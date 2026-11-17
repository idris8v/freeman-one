"""Shared browser request boundary and safe private-file response headers."""
from functools import lru_cache
from hashlib import sha256
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote

from anyio import to_thread

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles


class SecurityMiddleware:
    def __init__(self, app, web_base_url):
        self.app = app
        self.origin = web_base_url.rstrip('/')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path = scope.get('path', '')
        if scope['method'] not in ('GET', 'HEAD', 'OPTIONS'):
            origin = headers.get('origin')
            if headers.get('sec-fetch-site') == 'cross-site' or (origin and origin != self.origin):
                response = JSONResponse({'detail': 'Запрос должен отправляться с сайта Freeman ONE.'}, status_code=403)
                return await response(scope, receive, send)

        async def outgoing(message):
            if message['type'] == 'http.response.start':
                result = MutableHeaders(scope=message)
                result.setdefault('X-Content-Type-Options', 'nosniff')
                result.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
                if path.startswith('/api/'):
                    result['Cache-Control'] = 'no-store'
            await send(message)

        await self.app(scope, receive, outgoing)


class PublicStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        # AppleDouble metadata and other hidden files are never public assets.
        if any(part.startswith('.') for part in PurePosixPath(path).parts):
            from starlette.exceptions import HTTPException
            raise HTTPException(404)
        response = await super().get_response(path, scope)
        # Only content-addressed public CSS/JS may be cached indefinitely. An
        # old HTML document requesting a stale hash must revalidate, not cache
        # a different release under the previous version for a year.
        response.headers['Cache-Control'] = 'no-cache'
        version = parse_qs(scope.get('query_string', b'').decode('ascii', errors='ignore')).get('v', [])
        if response.status_code in (200, 304) and Path(path).suffix in ('.css', '.js') and len(version) == 1:
            full_path, stat = await to_thread.run_sync(self.lookup_path, path)
            if stat and version[0] == await to_thread.run_sync(_asset_version, full_path, stat.st_mtime_ns, stat.st_size):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response


@lru_cache(maxsize=256)
def _asset_version(path, modified_ns, size):
    # File identity is in the cache key; no private responses pass this path.
    return sha256(Path(path).read_bytes()).hexdigest()[:12]


def telegram_media_headers(kind, file_path):
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
    if kind == 'document':
        name = PurePosixPath(file_path or 'document').name or 'document'
        headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + quote(name, safe='')
        headers['Content-Security-Policy'] = "sandbox; default-src 'none'"
    return headers

"""Small authenticated client for the official Telemt Control API."""
import asyncio
import json
import urllib.error
import urllib.request
from urllib.parse import quote


class TelemtError(ValueError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status=status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelemtClient:
    def __init__(self, base_url, authorization, timeout=10):
        self.base_url=base_url.rstrip('/')
        self.authorization=authorization
        self.timeout=timeout

    def _request(self, method, path, body=None):
        data=json.dumps(body).encode() if body is not None else None
        headers={'Accept':'application/json'}
        if data is not None:
            headers['Content-Type']='application/json'
        if self.authorization:
            headers['Authorization']=self.authorization
        request=urllib.request.Request(self.base_url+path,data=data,headers=headers,method=method)
        try:
            with urllib.request.build_opener(_NoRedirect).open(request,timeout=self.timeout) as response:
                raw=response.read().decode('utf-8')
                payload=json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            try:
                payload=json.loads(error.read().decode('utf-8'))
                detail=payload.get('error') or payload.get('message') or f'HTTP {error.code}'
                if isinstance(detail,dict):
                    detail=detail.get('message') or detail.get('code') or f'HTTP {error.code}'
            except Exception:
                detail=f'HTTP {error.code}'
            raise TelemtError(f'Telemt API: {detail}',error.code)
        except (OSError,ValueError,json.JSONDecodeError) as error:
            raise TelemtError('Сервер Telemt не отвечает или вернул неверный ответ.') from error
        if isinstance(payload,dict) and payload.get('ok') is False:
            raise TelemtError('Telemt API отклонил запрос.')
        return payload.get('data',payload) if isinstance(payload,dict) else payload

    async def request(self, method, path, body=None):
        return await asyncio.to_thread(self._request,method,path,body)

    async def health(self):
        return await self.request('GET','/v1/health/ready')

    async def users(self):
        data=await self.request('GET','/v1/users')
        return data.get('users',[]) if isinstance(data,dict) else data

    async def user(self, username):
        return await self.request('GET','/v1/users/'+quote(username,safe=''))

    async def create_user(self, username, **values):
        return await self.request('POST','/v1/users',{'username':username,**values})

    async def patch_user(self, username, **values):
        return await self.request('PATCH','/v1/users/'+quote(username,safe=''),values)

    async def enable_user(self, username):
        return await self.request('POST','/v1/users/'+quote(username,safe='')+'/enable',{})

    async def disable_user(self, username):
        return await self.request('POST','/v1/users/'+quote(username,safe='')+'/disable',{})

    async def delete_user(self, username):
        return await self.request('DELETE','/v1/users/'+quote(username,safe=''))

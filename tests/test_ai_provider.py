import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app.services.freeman_ai.provider import ResponsesProvider, ProviderUnavailable

class Response:
    status=200
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def json(self):return {'status':'completed','output':[]}

class Session:
    def __init__(self,**kwargs):self.calls=[]
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def post(self,*args,**kwargs):self.calls.append((args,kwargs));return Response()

class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def settings(self,base='https://ai.starimg.ru/v1/'):
        return SimpleNamespace(freeman_ai_base_url=base,freeman_ai_openai_key='test-secret',freeman_ai_model='test-model')
    async def test_custom_endpoint_and_redirects_disabled(self):
        session=Session()
        with patch('app.services.freeman_ai.provider.aiohttp.ClientSession',return_value=session):
            await ResponsesProvider(self.settings()).complete([], 'test', [])
        args,kwargs=session.calls[0]
        self.assertEqual(args[0],'https://ai.starimg.ru/v1/responses')
        self.assertIs(kwargs['allow_redirects'],False)
        self.assertIs(kwargs['json']['store'],False)
        self.assertEqual(kwargs['headers']['Authorization'],'Bearer test-secret')
    def test_unsafe_urls_rejected_before_request(self):
        for base in ['http://example.com/v1','https://user:pass@example.com/v1','https://example.com/v1?key=x','https://example.com/v1#x','https:///v1','https://bad host/v1']:
            with self.subTest(base=base),self.assertRaises(ProviderUnavailable):ResponsesProvider(self.settings(base))
    def test_official_default_backwards_compatible(self):
        s=self.settings();del s.freeman_ai_base_url
        self.assertEqual(ResponsesProvider(s).endpoint,'https://api.openai.com/v1/responses')
    async def test_error_never_exposes_provider_body(self):
        session=Session()
        with patch('app.services.freeman_ai.provider.aiohttp.ClientSession',return_value=session),patch.object(Response,'status',401):
            with self.assertRaisesRegex(ProviderUnavailable,'^model_http_401$'):
                await ResponsesProvider(self.settings()).complete([], 'test', [])

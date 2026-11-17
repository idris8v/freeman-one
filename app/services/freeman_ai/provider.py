"""Optional Responses API transport, without database/server privileges."""
import aiohttp
from urllib.parse import urlsplit
class ProviderUnavailable(Exception):pass
class ResponsesProvider:
    def __init__(self,settings):
        self.settings=settings
        base=getattr(settings,'freeman_ai_base_url','https://api.openai.com/v1').rstrip('/')
        parsed=urlsplit(base)
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(c.isspace() for c in base)):
            raise ProviderUnavailable('invalid_provider_url')
        self.endpoint=base+'/responses'
    async def complete(self,items,instructions,tools):
        if not self.settings.freeman_ai_openai_key or not self.settings.freeman_ai_model:raise ProviderUnavailable('model_not_configured')
        body=dict(model=self.settings.freeman_ai_model,store=False,input=items,instructions=instructions,tools=tools,max_output_tokens=4000,parallel_tool_calls=False)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=55)) as client:
            async with client.post(self.endpoint,json=body,headers={'Authorization':'Bearer '+self.settings.freeman_ai_openai_key},allow_redirects=False) as r:
                if r.status!=200:raise ProviderUnavailable('model_http_'+str(r.status))
                data=await r.json()
        if data.get('status')!='completed':raise ProviderUnavailable('model_incomplete')
        return data

    async def complete_stream(self,items,instructions,tools,on_text):
        """One provider request. An interrupted/ambiguous request is never retried here."""
        import json
        if not self.settings.freeman_ai_openai_key or not self.settings.freeman_ai_model:
            raise ProviderUnavailable('model_not_configured')
        body=dict(model=self.settings.freeman_ai_model,store=False,input=items,
                  instructions=instructions,tools=tools,max_output_tokens=4000,
                  parallel_tool_calls=False,stream=True)
        timeout=aiohttp.ClientTimeout(total=55,sock_read=25)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(self.endpoint,json=body,headers={
                'Authorization':'Bearer '+self.settings.freeman_ai_openai_key,
                'Accept':'text/event-stream'},allow_redirects=False) as r:
                if r.status!=200:raise ProviderUnavailable('model_http_'+str(r.status))
                # Some compatible gateways return a complete JSON response even with stream=True.
                if r.headers.get('Content-Type','').split(';')[0].strip()=='application/json':
                    data=await r.json()
                    if not isinstance(data,dict) or data.get('status')!='completed':
                        raise ProviderUnavailable('model_incomplete')
                    return data
                text='';event=[];event_size=0;received=0
                async def consume(lines):
                    nonlocal text
                    payload='\n'.join(lines)
                    if not payload or payload=='[DONE]':return None
                    try:data=json.loads(payload)
                    except (TypeError,ValueError):raise ProviderUnavailable('model_stream_malformed')
                    if not isinstance(data,dict):raise ProviderUnavailable('model_stream_malformed')
                    kind=data.get('type')
                    if kind=='response.output_text.delta':
                        delta=data.get('delta')
                        if not isinstance(delta,str):raise ProviderUnavailable('model_stream_malformed')
                        text=(text+delta)[:12000]
                        await on_text(text)
                    elif kind in ('response.failed','response.incomplete','error'):
                        raise ProviderUnavailable('model_incomplete')
                    elif kind=='response.completed':
                        result=data.get('response')
                        if not isinstance(result,dict) or result.get('status')!='completed':
                            raise ProviderUnavailable('model_incomplete')
                        return result
                    return None
                async for raw in r.content:
                    received+=len(raw);event_size+=len(raw)
                    if received>4000000 or event_size>1000000:
                        raise ProviderUnavailable('model_stream_too_large')
                    try:line=raw.decode('utf-8').rstrip('\r\n')
                    except UnicodeError:raise ProviderUnavailable('model_stream_malformed')
                    if not line:
                        data=await consume(event)
                        if data is not None:return data
                        event=[];event_size=0
                    elif line.startswith('data:'):
                        event.append(line[5:].lstrip(' '))
                if event:
                    data=await consume(event)
                    if data is not None:return data
        raise ProviderUnavailable('model_stream_interrupted')

"""Responses transport for the support agent and its server-managed backups."""
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from app.services.freeman_ai.customer_config import CustomerSettings
from app.services.freeman_ai.provider import ResponsesProvider as Base, ProviderUnavailable


LOG = logging.getLogger(__name__)
_BACKUP_CONFIG = Path('/run/freeman-ai/customer-providers.json')
_UNAVAILABLE = (TimeoutError, aiohttp.ClientError, ProviderUnavailable)
_COOLDOWN = {}


@dataclass(frozen=True)
class Profile:
    name: str
    endpoint: str
    model: str
    key: str = field(repr=False)

    @property
    def identity(self):
        return self.endpoint, self.model, hashlib.sha256(self.key.encode()).digest()


def backup_profiles():
    try:
        records = json.loads(_BACKUP_CONFIG.read_text())
    except FileNotFoundError:
        return ()
    except (OSError, ValueError):
        LOG.warning('Customer AI backup configuration is unreadable')
        return ()
    if not isinstance(records, list) or len(records) > 2:
        LOG.warning('Customer AI backup configuration has an invalid profile count')
        return ()
    profiles = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        values = [record.get(key) for key in ('base_url', 'model', 'api_key')]
        if not all(isinstance(value, str) and value.strip() for value in values):
            continue
        base, model, key = (value.strip() for value in values)
        try:
            endpoint = Base(SimpleNamespace(freeman_ai_base_url=base)).endpoint
        except ProviderUnavailable:
            LOG.warning('Customer AI backup slot %s has an invalid endpoint', index + 1)
            continue
        # Labels are used for diagnostics. Keep arbitrary configuration out of logs.
        name = 'backup' if index == 0 else 'backup-2'
        profiles.append(Profile(name, endpoint, model, key))
    return tuple(profiles)


def transferable_input(items):
    """Carry conversation/tool state across providers without opaque reasoning."""
    return [
        {key: value for key, value in item.items() if key not in ('id', 'status')}
        if item.get('type') in ('message', 'function_call') else item
        for item in items if item.get('type') != 'reasoning'
    ]


class ResponsesProvider(Base):
    def __init__(self, settings):
        super().__init__(settings)
        primary = Profile('primary', self.endpoint, settings.freeman_ai_model, settings.freeman_ai_openai_key)
        backups = backup_profiles() if isinstance(settings, CustomerSettings) else ()
        profiles = {primary.identity: primary}
        for profile in backups:
            profiles.setdefault(profile.identity, profile)
        self.profiles = tuple(profiles.values())
        self.preferred = primary
        self.portable_turn = False
        self.quick_reply_timeout = 20 if len(self.profiles) > 1 else 8

    async def _request(self, profile, items, instructions, tools, timeout):
        if not profile.key or not profile.model:
            raise ProviderUnavailable('model_not_configured')
        body = dict(
            model=profile.model, store=False, input=items, instructions=instructions, tools=tools,
            max_output_tokens=2200, parallel_tool_calls=False,
        )
        if tools:
            body['tool_choice'] = 'required'
        if profile.model == 'gpt-6-luna':
            body['reasoning'] = {'effort': 'low'}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),trust_env=False) as client:
            async with client.post(
                profile.endpoint, json=body, headers={'Authorization':'Bearer '+profile.key}, allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ProviderUnavailable('model_http_'+str(response.status))
                try:
                    result = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise ProviderUnavailable('model_invalid_json') from None
        if not isinstance(result, dict) or not isinstance(result.get('output'), list):
            raise ProviderUnavailable('model_invalid_json')
        if result.get('status') != 'completed':
            raise ProviderUnavailable('model_incomplete')
        if not any(isinstance(item, dict) and item.get('type') in ('message', 'function_call') for item in result['output']):
            raise ProviderUnavailable('empty_model_reply')
        return result

    async def complete(self, items, instructions, tools):
        if len(self.profiles) == 1:
            return await self._request(self.preferred, items, instructions, tools, 55)

        candidates = [self.preferred, *(p for p in self.profiles if p != self.preferred)]
        now = time.monotonic()
        for identity in tuple(_COOLDOWN):
            if _COOLDOWN[identity] <= now:
                del _COOLDOWN[identity]
        available = [p for p in candidates if p.identity not in _COOLDOWN]
        if not available:
            # Let the first recovered route serve the next message.
            available = [min(candidates, key=lambda p: _COOLDOWN[p.identity])]

        for index, profile in enumerate(available):
            switching = profile != self.preferred
            request_items = transferable_input(items) if switching or self.portable_turn else items
            last = index == len(available) - 1
            try:
                reply = await self._request(profile, request_items, instructions, tools, 10 if last else 5)
            except _UNAVAILABLE as error:
                from app.services.freeman_ai.recovery import failure_code
                code = failure_code(error)
                _COOLDOWN[profile.identity] = time.monotonic() + (90 if code in (
                    'model_http_401', 'model_http_402', 'model_http_403', 'model_http_429',
                ) else 30)
                LOG.warning('Customer AI route failed: slot=%s outcome=%s', profile.name, code)
                if last:
                    raise
            else:
                if switching:
                    self.portable_turn = True
                    LOG.info('Customer AI route switched: slot=%s', profile.name)
                self.preferred = profile
                return reply
        raise ProviderUnavailable('model_not_configured')

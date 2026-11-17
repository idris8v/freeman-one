"""Owner-only bounded dictation. Audio stays in memory, never in customer storage/logs."""
import asyncio
import hashlib
import io
import wave
import re
import time
from collections import deque
from urllib.parse import urlsplit
import aiohttp
from fastapi import HTTPException
from fastapi.responses import JSONResponse

MAX_BYTES = 6 * 1024 * 1024
FORMATS = {'audio/wav': ('wav', b'RIFF'), 'audio/x-wav': ('wav', b'RIFF')}

class Dictation:
    def __init__(self):
        self.active = set()
        self.attempts = {}
        self.cache = {}

    async def response(self, request, settings, uid):
        key = request.headers.get('x-dictation-id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', key):
            raise HTTPException(422, 'Не указан идентификатор записи.')
        mime = request.headers.get('content-type', '').split(';')[0].lower()
        if mime not in FORMATS:
            raise HTTPException(415, 'Браузер должен преобразовать запись в WAV перед распознаванием.')
        size = request.headers.get('content-length')
        if size and (not size.isdigit() or int(size) > MAX_BYTES):
            raise HTTPException(413, 'Запись должна быть не больше 6 МБ.')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, 'Запись должна быть не больше 6 МБ.')
        if len(data) < 32:
            raise HTTPException(422, 'Запись пуста или повреждена.')
        ext, magic = FORMATS[mime]
        if (magic and not data.startswith(magic)) or (mime == 'audio/mp4' and data[4:8] != b'ftyp'):
            raise HTTPException(422, 'Формат записи не соответствует её содержимому.')
        try:
            with wave.open(io.BytesIO(data), 'rb') as audio:
                frames=audio.getnframes()
                if (audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=16000
                        or frames<=0 or frames>180*16000 or len(audio.readframes(frames))!=frames*2):
                    raise ValueError('invalid_audio')
        except (wave.Error, EOFError, ValueError):
            raise HTTPException(422, 'Нужна неповреждённая запись WAV: 16 кГц, моно, до 3 минут.') from None
        clock = time.monotonic()
        self.cache = {k: v for k, v in self.cache.items() if v[0] > clock}
        digest = hashlib.sha256(data).hexdigest()
        prior = self.cache.get((uid, key))
        if prior:
            if prior[1] != digest:
                raise HTTPException(409, 'Идентификатор уже использован для другой записи.')
            return JSONResponse({'text': prior[2]}, headers={'Cache-Control': 'no-store'})
        if uid in self.active or len(self.active) >= 3:
            raise HTTPException(429, 'Распознавание уже выполняется. Подождите.')
        window = self.attempts.setdefault(uid, deque())
        while window and window[0] < clock - 60:
            window.popleft()
        if len(window) >= 6:
            raise HTTPException(429, 'Слишком много записей. Повторите через минуту.')
        window.append(clock)
        self.active.add(uid)
        try:
            text = await transcribe(settings, bytes(data), mime, ext)
            self.cache[(uid, key)] = (time.monotonic() + 120, digest, text)
            return JSONResponse({'text': text}, headers={'Cache-Control': 'no-store'})
        finally:
            self.active.discard(uid)

async def transcribe(settings, data, mime, ext):
    base = settings.freeman_ai_base_url.rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in base)
            or not settings.freeman_ai_openai_key):
        raise HTTPException(503, 'Сервер распознавания не настроен.')
    form = aiohttp.FormData()
    form.add_field('model', 'gpt-4o-transcribe')
    form.add_field('response_format', 'json')
    form.add_field('file', data, filename='dictation.' + ext, content_type=mime)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=55, sock_read=45)) as client:
            async with client.post(base + '/audio/transcriptions', data=form,
                    headers={'Authorization': 'Bearer ' + settings.freeman_ai_openai_key},
                    allow_redirects=False) as response:
                if response.status != 200:
                    raise HTTPException(502, 'Сервис распознавания временно недоступен. Запись сохранена до закрытия страницы; можно повторить.')
                raw = await response.content.read(100001)
                if len(raw) > 100000:
                    raise ValueError('oversize')
                import json
                result = json.loads(raw)
                text = result.get('text') if isinstance(result, dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise HTTPException(422, 'Речь не распознана. Попробуйте говорить ближе к микрофону.')
                if len(text) > 3500:
                    raise HTTPException(422, 'Текст длиннее 3500 символов. Запишите сообщение короче.')
                return text.strip()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise HTTPException(502, 'Не удалось распознать запись. Повторите попытку; сообщение в чат не отправлено.') from None

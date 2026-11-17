"""Telegram Mini App entry for post-purchase installation support."""
from hashlib import sha256
from urllib.parse import urlsplit

from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.database.models import AccessKey, Ticket, TicketMessage, User, VlessCredential, now
from app.database.repositories import SQLRepository
from app.services.tickets import TicketService
from app.services.vless_creation import utc


PATH = '/bot/install-help'
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}
PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Freeman ONE · Помощь с установкой</title>
<script src="https://telegram.org/js/telegram-web-app.js?65"></script>
</head><body style="font:16px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;
color:var(--tg-theme-text-color,#16181d);background:var(--tg-theme-bg-color,#fff);padding:24px">
<p id="status" role="status">💬 Открываем чат поддержки…</p>
<script>
(async () => {
    const app = window.Telegram?.WebApp;
    const status = document.getElementById('status');
    app?.ready();
    try {
        const sid = new URLSearchParams(location.search).get('sid');
        if (!app?.initData || !sid) throw new Error('Откройте помощь кнопкой в Telegram-боте.');
        const response = await fetch(location.pathname + '/' + encodeURIComponent(sid), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({init_data: app.initData})
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '');
        location.replace(payload.chat);
    } catch (error) {
        status.textContent = error.message || 'Не удалось открыть чат. Попробуйте ещё раз из бота.';
    }
})();
</script></body></html>"""


class Launch(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


def help_target(settings, sid):
    return f'webapp:{settings.web_base_url.rstrip("/")}{PATH}?sid={sid}'


async def open_chat(repo, settings, telegram_id, sid, launch_id):
    user = await repo.session.scalar(select(User).where(
        User.telegram_id == telegram_id, User.status == 'active',
    ).with_for_update())
    sub = await repo.subscription(user.id, sid) if user else None
    credential = await repo.session.scalar(select(VlessCredential).where(
        VlessCredential.subscription_id == sid, VlessCredential.user_id == user.id,
    )) if sub else None
    key = await repo.session.get(AccessKey, credential.access_key_id) if credential else None
    if (not sub or not credential or not key or credential.status != 'active'
            or key.status != 'active' or utc(sub.expires_at) <= now()
            or await repo.setting(f'subscription_removed:{sid}') == '1'
            or await repo.setting(f'subscription_status:{sid}', 'active') != 'active'
            or not (credential.subscription_url or '').startswith('https://')):
        raise HTTPException(404, 'Подписка недоступна. Откройте помощь из карточки действующей подписки.')

    # Reopen the existing installation chat while it is active. The user lock
    # serializes launches and TicketService also deduplicates individual retries.
    prefix = f'web:bot-install:{user.id}:{sid}:'
    message = ('Здравствуйте! Подписка уже оплачена. Помогите, пожалуйста, установить VPN.\n\n'
               f'Ссылка на подписку: {credential.subscription_url}')
    ticket_id = await repo.session.scalar(select(Ticket.id).join(TicketMessage).where(
        Ticket.user_id == user.id, Ticket.status != 'closed', TicketMessage.source.startswith(prefix),
        TicketMessage.text == message,
    ).order_by(Ticket.id.desc()).limit(1))
    tickets = TicketService(repo, settings)
    fingerprint = sha256(launch_id.encode()).hexdigest()[:32]
    source = prefix + fingerprint
    screen = (await tickets.render(f't:ticket:{ticket_id}:0', user) if ticket_id
              else await tickets.input('t:input:new', user, message, source, force_new=True))
    chat = urlsplit(screen.rows[0][0][1].removeprefix('webapp:'))
    return f'{chat.path}?{chat.query}'


def router(database, settings):
    result = APIRouter()

    @result.get(PATH, include_in_schema=False)
    async def page():
        return HTMLResponse(PAGE, headers=HEADERS)

    @result.post(PATH + '/{sid}', include_in_schema=False)
    async def launch(sid: int, payload: Launch):
        try:
            data = safe_parse_webapp_init_data(settings.bot_token, payload.init_data)
            age = (now() - utc(data.auth_date)).total_seconds()
            if not settings.bot_token or not data.user or not -30 <= age <= 300:
                raise ValueError
        except (ValueError, OverflowError):
            raise HTTPException(403, 'Откройте помощь заново кнопкой в Telegram-боте.') from None
        async with database.sessions.begin() as session:
            launch_id = data.query_id or str(data.auth_date.timestamp())
            chat = await open_chat(SQLRepository(session), settings, data.user.id, sid, launch_id)
        return JSONResponse({'chat': chat}, headers=HEADERS)

    return result

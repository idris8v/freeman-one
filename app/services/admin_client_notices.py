"""Private Telegram receipts for confirmed administrator changes."""
import json
from html import escape

from app.database.models import ChatNotice, ProxyCredential, User, now
from app.services.admin_client_grant import rich
from app.services.vless_creation import utc


async def queue(repo, user, actor, reference, title, body='', *, button=None, proxy=None):
    if not user or not user.telegram_id or user.telegram_id <= 0 or user.id == actor.id:
        return
    dedup = f'admin-client:{reference}:{user.id}'
    from sqlalchemy import select
    if await repo.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup == dedup)):
        return
    value = json.dumps(dict(user=user.id, title=title, body=body, button=button, proxy=proxy), ensure_ascii=False)
    await repo.add(ChatNotice(dedup=dedup,target_id=user.telegram_id,kind='admin_client_change',text=value))


async def screen(repo, settings, job):
    data = json.loads(job.text)
    user = await repo.session.get(User, data['user'])
    if not user or user.telegram_id != job.target_id:
        return None
    rows = [[tuple(data['button'])]] if data.get('button') else []
    if data.get('proxy'):
        proxy = await repo.session.get(ProxyCredential,data['proxy'])
        if not proxy or proxy.user_id != user.id or proxy.status != 'active' or utc(proxy.expires_at) <= now() or not proxy.link:
            return None
        from app.services.proxies import ProxyService
        service = ProxyService(repo,settings)
        link = service.public_link(service.decrypt(proxy.link))
        rows = [[('✈️ Подключить прокси','url:'+link)], [('📋 Скопировать ссылку','copy:'+link)],
                [('🌐 Мои прокси','p:home')]]
        intro = 'Telegram можно использовать без включённого VPN. Нажмите «Подключить прокси» и подтвердите подключение в приложении.'
        result = rich(f'<b>{escape(data["title"])}</b>\n\n{intro}\n\n<blockquote>{escape(data["body"])}</blockquote>', rows)
        result.button_options['url:'+link] = {'style':'primary'}
        return result
    title = data['title']
    if not title[:1] or title[0].isalnum():
        title = 'ℹ️ '+title
    body = '\n\n<blockquote>'+escape(data['body'])+'</blockquote>' if data['body'] else ''
    if data.get('button') and data['button'][1]=='balance':
        body += '\n\n💡 Баланс можно использовать для оплаты подписки.'
    return rich(f'<b>{escape(title)}</b>'+body, rows)

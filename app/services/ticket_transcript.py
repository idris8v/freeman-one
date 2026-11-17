"""Private, self-contained ticket transcript; no remote resources or scripts."""
from html import escape

def render_html(ticket,messages,status):
    bubbles=[]
    for message in messages:
        role='client' if message.role=='client' else 'system' if message.role=='system' else 'support'
        name={'client':'Вы','system':'Система','support':'Поддержка Freeman ONE'}[role]
        attachment='📎 В сообщении есть вложение. Откройте его в Telegram или личном кабинете.' if message.photo or message.document else ''
        bubbles.append(f'<article class="message {role}"><strong>{name}</strong><p>{escape(message.text or "")}</p>'
                       f'{"<p class=attachment>"+attachment+"</p>" if attachment else ""}'
                       f'<time>{message.created_at:%d.%m.%Y %H:%M} UTC</time></article>')
    body=''.join(bubbles) or '<p class="empty">Сообщений пока нет.</p>'
    return ("""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Freeman ONE — переписка с поддержкой</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#121722;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}
main{max-width:800px;margin:auto;padding:24px 16px 48px}header{background:#fff;padding:22px 24px;border-radius:24px;box-shadow:0 4px 18px #192b4810;margin-bottom:28px}
h1{font-size:23px;margin:0 0 6px}header p{color:#737d90;margin:0}.history{display:flex;flex-direction:column;gap:16px}.message{max-width:86%;padding:16px 20px;border-radius:22px;background:white;align-self:flex-start;box-shadow:0 3px 12px #192b4809;overflow-wrap:anywhere}
.message.client{align-self:flex-end;background:#087fff;color:white}.message.system{align-self:center;background:#e9edf4;color:#677285;font-size:13px;box-shadow:none}
.message strong{font-size:13px}.message p{white-space:pre-wrap;margin:8px 0 10px}.message time{display:block;font-size:11px;opacity:.7;text-align:right}.attachment{font-size:13px;opacity:.85}footer{text-align:center;font-size:12px;color:#7a8596;margin-top:30px}
@media(max-width:480px){main{padding:12px 10px 30px}.message{max-width:94%;padding:14px 16px}h1{font-size:20px}}@media(prefers-color-scheme:dark){body{background:#101318;color:#eff2f8}header,.message{background:#202630}.message.client{background:#066cdb}.message.system{background:#252d38;color:#b3bdcc}header p,footer{color:#a8b2c2}}
</style></head><body><main>"""+f'<header><h1>💬 Поддержка Freeman ONE</h1><p>Обращение №{ticket.id} · {escape(status)}</p></header><section class="history" aria-label="Переписка">'+body+
'</section><footer>Полная сохранённая переписка · Время указано в UTC<br>Это копия чата. Для ответа откройте поддержку в Telegram или личном кабинете.</footer></main></body></html>').encode('utf-8')

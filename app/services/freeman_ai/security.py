import re, hashlib, hmac
from cryptography.fernet import Fernet
from datetime import timezone

URL=re.compile(r'(?:https?://|vless://|vmess://|trojan://|ss://)[^\s<>"\']+',re.I)
EMAIL=re.compile(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b')
PHONE=re.compile(r'(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)')
CREDENTIAL=re.compile(r'(?i)\b(?:password|пароль|token|токен|api[_ -]?key)\s*[:=]\s*\S+')
def redact(value):
    text=URL.sub('[ссылка скрыта]',str(value or ''))
    text=EMAIL.sub('[email скрыт]',text);text=PHONE.sub('[номер скрыт]',text)
    return CREDENTIAL.sub('[секрет скрыт]',text)[:6000]
def chat_text(value):
    """Keep connection links in private conversations, not in operational logs.

    Process URL spans separately so a VLESS username or long numeric path is
    not mistaken for a contact address/phone. Explicit credential assignments
    outside links still use the existing secret redactor.
    """
    text = str(value or '')
    links = list(URL.finditer(text))
    # A subscription may legitimately use ?token=... or a credential URI.
    # Only redact explicit credential assignments outside the URL itself.
    def credential(match):
        if any(link.start() <= match.start() < link.end() for link in links):
            return match.group(0)
        return '[секрет скрыт]'
    text = CREDENTIAL.sub(credential, text)
    pieces, start = [], 0
    for match in URL.finditer(text):
        pieces.append(redact(text[start:match.start()]))
        pieces.append(match.group(0))
        start = match.end()
    pieces.append(redact(text[start:]))
    return ''.join(pieces)[:6000]

def fingerprint(settings,value):return hmac.new(settings.encryption_key.encode(),value.encode(),hashlib.sha256).hexdigest()
def encrypt(settings,value):return Fernet(settings.encryption_key.encode()).encrypt(value.encode()).decode()
def decrypt(settings,value):return Fernet(settings.encryption_key.encode()).decrypt(value.encode()).decode()
def aware(value):return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
def channel_subject(channel,subject):
    subject=str(subject).strip()
    if channel not in ('telegram','whatsapp'):raise ValueError('Unsupported channel')
    if not re.fullmatch(r'\d{1,20}',subject):raise ValueError('Invalid channel identity')
    return subject

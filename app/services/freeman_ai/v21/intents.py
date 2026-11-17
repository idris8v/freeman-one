"""Conservative routing for an unambiguous request to pay."""
import re

_CANNOT_PAY = re.compile(r'\b(?:не получается|не могу|не уда[её]тся|не удалось)\s+(?:мне\s+)?оплатить\b')
_PAYMENT_RISK = re.compile(r'деньг|спис|чек|возврат|дважды|плат[её]ж|квитанц|ошибоч|уже оплат|оплатил|сняли')
_NEW_TOPIC = re.compile(r'продл|антиглушил|прокси|оператор|специалист|сумм|цен[аыу]')
_PURCHASE_LOCATION = re.compile(r'(?:где|как)\s+(?:мне\s+)?(?:купить|оформить)\s+(?:новую\s+)?(?:подписку|ключ|vpn|впн)|купить\s+(?:ключ\s+)?без регистрации')
_OLD_BOT = re.compile(r'стар\w*\s+(?:телеграм[ -])?бот')

def financial_risk(text):
    return bool(re.search(r'деньг|спис|чек|возврат|плат[её]ж|квитанц|оплат|сняли|баланс',text,re.I))

def payment_help(text, *, financial_request='', has_attachment=False):
    """Return PAY-01 only if a new checkout is safe to suggest."""
    text = text.casefold()
    return (
        not has_attachment and not financial_request
        and bool(_CANNOT_PAY.search(text) or _PURCHASE_LOCATION.search(text)
                 or (_OLD_BOT.search(text) and 'оплат' in text))
        and not _PAYMENT_RISK.search(text)
        and not _NEW_TOPIC.search(text)
    )

_SUBSCRIPTION_LIST = re.compile(
    r'(?:(?:где|покаж(?:и|ите)|посмотреть|открыть)\s+)?'
    r'(?:найти\s+)?(?:мои\s+)?(?:подписки|ключи)[.!?]*',
    re.IGNORECASE,
)

def subscription_list(text):
    return bool(_SUBSCRIPTION_LIST.fullmatch(text.strip()))

_GENERIC_VPN_FAILURE = re.compile(
    r'(?:у меня )?(?:vpn|впн) не работает(?:[.!]?\s+у меня (iphone|android))?[.!]?',
    re.IGNORECASE,
)

SCREENSHOT_REPLY = (
    '📸 **Пришлите скриншот из приложения VPN**\n\n'
    '1️⃣ Откройте приложение, в котором подключаете VPN.\n\n'
    '2️⃣ Сделайте скриншот главного экрана приложения.\n\n'
    '3️⃣ Отправьте скриншот сюда, в чат.\n\n'
    '📌 На скриншоте должны быть видны **наша подписка** и **кнопка подключения VPN**.'
)


def first_vpn_diagnostic(text):
    """SERVICE-06 when the complaint has no screenshot and no prior steps."""
    match = _GENERIC_VPN_FAILURE.fullmatch(text.strip())
    if not match:
        return None
    platform = match.group(1)
    model = f'модель {platform}' if platform else 'модель телефона'
    return SCREENSHOT_REPLY + f'\n\nЕсли можете, напишите {model}. В приложении наша подписка называется **FreemanOne**.'


_MESSENGER = re.compile(r'\b(?:telegram|телеграм\w*|телега|тг|whats\s*app|ватсап\w*|ватцап\w*|вотсап\w*)\b')
_FAILURE = re.compile(
    r'\b(?:не\s+(?:работ\w*|открыва\w*|груз\w*|загружа\w*|отправля\w*|приход\w*|подключа\w*|звон\w*)'
    r'|переста(?:л[аио]?)\s+(?:работать|открывать\w*|загружать\w*)|тормоз\w*|завис\w*|сбо[йяи]|лага\w*|не слыш\w*)\b'
)
_OTHER_REQUEST = re.compile(r'бот\b|оплат|списал|возврат|подписк[ауи] не приш|заблокирован аккаунт')
_OPERATOR_REQUEST = re.compile(
    r'(?:\b(?:позов\w*|подключ\w*|переда\w*|перевед\w*|позови|дайте|можно|хочу|нужен|свяжите)[, ]+'
    r'(?:мне\s+|меня\s+|с\s+|к\s+|на\s+|чат\s+|диалог\s+|пожалуйста[, ]+|техническ\w*\s+)*(?:оператор\w*|специалист\w*|человек\w*|администратор\w*)'
    r'|^\s*(?:оператор|специалист)(?:,?\s+пожалуйста)?[.!?\s]*$)'
)


def messenger_issue(text):
    text = text.casefold()
    match = _MESSENGER.search(text)
    if not match or not _FAILURE.search(text) or _OTHER_REQUEST.search(text):
        return None
    if re.search(r'(?:vpn|впн|ключ)\s+не\s+работ', text) and not re.search(r'только|звонк|сообщени|медиа', text):
        return None
    return 'WhatsApp' if match.group().startswith(('whats', 'ват', 'вот')) else 'Telegram'


def operator_requested(text):
    text = text.casefold()
    return bool(_OPERATOR_REQUEST.search(text) and not re.search(
        r'\bне\s+(?:надо|нужно|хочу|зовите|передавайте|подключайте)\b[^.!?]*(?:оператор|специалист|администратор|человек)',
        text,
    ))


def technical_issue(text):
    text = text.casefold()
    if re.fullmatch(r'\s*(?:у меня\s+)?(?:вообще\s+)?(?:ничего\s+)?не\s+работает[.!\s]*', text):
        return True
    return bool(_FAILURE.search(text) and not _OTHER_REQUEST.search(text) and re.search(
        r'vpn|впн|happ|incy|rabbit|интернет|сервер|пинг|wi-?fi|приложен|сайт|youtube|ютуб|gemini|инстаграм|сообщени|звонки|прокси',
        text,
    ))

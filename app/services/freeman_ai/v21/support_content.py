"""Verified replies for clear intents. Client payloads cannot add links or attachments."""
import re
from dataclasses import dataclass

APPLE_REGION = 'https://support.apple.com/ru-ru/118283'
REGION_GUIDE = (
    '🌎 **Как сменить регион App Store на iPhone**\n\n'
    'Посмотрите короткое видео. Ниже те же шаги, чтобы можно было сверяться по ходу.\n\n'
    'Перед началом проверьте баланс Apple, активные подписки и семейный доступ. '
    'Они могут мешать смене региона.\n\n'
    '1️⃣ Откройте **Настройки** → нажмите на своё имя сверху.\n\n'
    '2️⃣ Откройте **Медиаматериалы и покупки**. Если появится меню, нажмите **Просмотреть**.\n\n'
    '3️⃣ Нажмите **Страна/регион** → **Изменить страну или регион**. Выберите Казахстан, '
    'нажмите «Продолжить» и примите условия.\n\n'
    '4️⃣ Если Apple запросит оплату и адрес, введите свои данные в настройках Apple. '
    'Поля в видео приведены для примера.\n\n'
    '5️⃣ Откройте App Store снова. Когда регион сменится, напишите «Регион изменён» '
    '— дам кнопки скачивания для Казахстана. 🙂'
)
REGION_PENDING = 'Подтвердите, когда App Store переключён на Казахстан'
REGION_QUESTION = (
    '🌎 Если App Store пишет, что приложение недоступно в вашей стране, '
    'сначала уточним регион. У вас уже выбран **Казахстан**?\n\n'
    'Если ещё нет, откройте инструкцию с видео 👇'
)
REGION_APPS = (
    ('Happ', 'https://apps.apple.com/kz/app/happ-proxy-utility/id6504287215'),
    ('INCY', 'https://apps.apple.com/kz/app/incy/id6756943388'),
)

HAPP_IPHONE_INSTALL = (
    '🔑 **Как добавить ключ в Happ**\n\n'
    '1️⃣ Скопируйте целиком ссылку, которую получили после покупки. Это ваш ключ.\n\n'
    '2️⃣ Откройте Happ 📱\n\n'
    '3️⃣ На главном экране нажмите **«+» в правом верхнем углу**.\n\n'
    '4️⃣ В меню нажмите **«Добавить из буфера обмена»** 📋\n\n'
    '✅ В списке появится **FreemanOne**.\n\n'
    '5️⃣ Выберите сервер **«Авто»**, если он есть, или другой доступный сервер 🌐\n\n'
    '6️⃣ На главном экране Happ нажмите кнопку подключения. Если её не видно, пришлите скриншот — покажу, куда нажать.\n\n'
    '🎉 Когда VPN подключится, попробуйте открыть нужный сайт. '
    'Если не открывается только он, переключитесь на другой доступный сервер 🔄'
)

_INSTALL_QUESTION = re.compile(r'\bкак\s+(?:мне\s+)?(?:добавить|вставить|установить)\s+(?:(?:свой|мой|ваш|этот)\s+)?(?:ключ|подписку)\b')
_INSTALL_EXCEPTION = re.compile(r'не\s+(?:получ|работ|вижу|наш[её]л|добав)|ошиб|смен|перенос|меняю|другое прилож|нов(?:ый|ом) телефон|android|андроид|incy|rabbit|windows')

def happ_install_reply(text,state):
    """Use the checked iPhone route only for an explicit installation request."""
    folded=text.casefold()
    if not _INSTALL_QUESTION.search(folded) or _INSTALL_EXCEPTION.search(folded):
        return None
    app=folded+' '+state.application.casefold()
    os=folded+' '+state.os.casefold()+' '+state.device_model.casefold()
    if re.search(r'\bhapp\b|\bх[эе]пп?\b',app) and re.search(r'\biphone\b|\bios\b|айфон',os):
        return HAPP_IPHONE_INSTALL
    return None

def region_reply():
    return REGION_GUIDE, [
        dict(type='guide_video', material='appstore-region'),
        dict(type='link', label='Инструкция Apple', url=APPLE_REGION),
    ]

def region_intent(text, state, has_apple_context=False):
    text = text.casefold().strip(' .!?')
    if re.search(r'windows|android|виндовс|андроид|регион сервер|регион vpn|регион телеграм',text):
        return None
    if re.search(r'\bкак\s+(?:можно\s+)?(?:сменить|поменять|изменить)\s+(?:страну|регион)\b', text) or text in (
        'смена региона app store', 'инструкция по смене региона', 'смена региона',
    ):
        return 'guide'
    if re.search(r'\b(?:регион (?:уже )?(?:изменён|изменен|сменён|сменен|казахстан)|(?:сменил[аи]?|изменил[аи]?|поменял[аи]?) регион)\b', text):
        if state.expected_answer == REGION_PENDING or 'казахстан' in text:
            return 'downloads'
    if state.expected_answer == REGION_PENDING and text in ('да', 'да, сменил', 'да, сменила'):
        return 'downloads'
    if re.search(r'недоступн\w*.{0,50}(?:стран|регион)', text) or (
        'недоступ' in text and (has_apple_context or re.search(r'app\s*store|iphone|айфон', text))
    ):
        return 'question'
    return None

_IMPORT_CONTEXT = re.compile(r'импорт|добав|обнов|ссылк|к[л]юч|happ|incy|подписк|\bhttp\b|\bapi\b', re.I)
_HTTP_CODE = re.compile(r'(?<!\d)(400|401|403|404|408|409|410|422|429|50[0-4])(?!\d)')
_ERROR_CONTEXT = re.compile(r'ошибк|\berror\b|\bhttp\b|\bapi\b|\bкод\b|пишет|выда[её]т|показывает',re.I)

def import_error(text):
    """Explain a reported import error without guessing the key's current status."""
    code = _HTTP_CODE.search(text)
    if not code or not _IMPORT_CONTEXT.search(text) or not _ERROR_CONTEXT.search(text) or re.search(r'оплат|списа|возврат|баланс', text, re.I):
        return None
    status = int(code[1])
    if status in (401,403):
        return f'🔑 **Ошибка {status}.** Приложение не получило доступ к подписке. По одному коду срок определить нельзя.\n\nПришлите полную ссылку Freeman ONE — проверю её.'
    if status in (404,410):
        return f'🔗 **Ошибка {status}.** По этой ссылке подписка не открылась. Если ключ перевыпускали, скопируйте новую ссылку из «Моих ключей».\n\nЕсли ссылка свежая, пришлите её сюда — проверю.'
    if status == 429:
        return '⏳ **Слишком много запросов.** Если приложение показало время ожидания, дождитесь его и обновите подписку один раз. Если ошибка останется, пришлите скриншот.'
    if status >= 500:
        return f'🛠 **Ошибка {status}.** Сервер не обработал запрос. Если ошибка повторяется, пришлите скриншот и примерное время — проверим на стороне сервиса.'
    if status in (400,422):
        return '🔗 Скопируйте полную ссылку подписки заново и добавьте её из буфера обмена. Если ошибка повторится, пришлите скриншот и название приложения.'
    if status == 408:
        return '🌐 Приложение не дождалось ответа. Отключите другой VPN, попробуйте вторую сеть и обновите подписку один раз. Осталась ошибка — пришлите скриншот.'
    return '🔄 Приложение столкнулось с конфликтом состояния. Обновите подписку один раз и пришлите скриншот, если ошибка останется.'

SCOPE_QUESTION = '🙂 **Что именно не работает с VPN?**\n\nВыберите один вариант 👇'
SCOPE_OPTIONS = ('Не работает что-то конкретное', 'Вообще ничего не работает')


_SCOPE_SERVICES = {
    'YouTube': r'youtube|ютуб',
    'WhatsApp': r'whats\s*app|ват[сц]ап|вотсап',
    'Telegram': r'telegram|телеграм',
    'Gemini': r'gemini|джемини',
    'Instagram': r'instagram|инстаграм',
}


def scope_followup(state, choice, recent_client_text):
    state.record_check('Масштаб проблемы', 'VPN', SCOPE_OPTIONS[choice])
    if choice == 1:
        title = '🌐 **Проверим подключение целиком**'
        question = 'Какую страну вы сейчас выбрали в VPN? Если уже пробовали другие серверы, напишите какие — учту это.'
    else:
        complaints = [
            text.casefold() for text in recent_client_text
            if isinstance(text, str) and re.search(r'не (?:работает|открывается|грузится|загружается)|ошибка', text.casefold())
        ]
        services = [
            name for name, aliases in _SCOPE_SERVICES.items()
            if any(re.search(rf'(?<!\w)(?:{aliases})(?!\w)', text) for text in complaints)
        ]
        if len(services) == 1:
            title = f'📱 **Проверим {services[0]}**'
            question = 'Что именно происходит при открытии? Если видите ошибку, можно приложить скриншот 📸'
        else:
            title = '📱 **Какой сайт или приложение не открывается?**'
            question = 'Напишите название, например YouTube, WhatsApp или Gemini. Если на экране есть ошибка, можно приложить скриншот 📸'
    state.expected_answer = question
    return title + '\n\n' + question

# Short, channel-neutral instructions that match the public connection guide.
CONNECTION_STEPS = {
    'rabbit': ('🐇 **Как добавить подписку в RabbitHole**',
        'Скопируйте ссылку Freeman ONE целиком.',
        'Откройте «Конфиги» и нажмите **«+»** справа вверху.',
        'В шторке «Добавить конфиг» выберите **«Буфер обмена»**.',
        'Если видите «Настроить VPN», нажмите эту кнопку и выберите «Разрешить» в системном окне.',
        'Выберите «Авто» и нажмите тёмную кнопку **«Подключиться»** внизу.'),
    'incy': ('📱 **Как добавить подписку в INCY**',
        'Скопируйте ссылку Freeman ONE целиком.',
        'Откройте INCY. На экране подключения нажмите **«Вставить»**, если видите эту кнопку. Если её нет, откройте **Добавить** и выберите импорт из буфера обмена.',
        'Если подписки не видно, откройте **Серверы** в нижней панели и найдите Freeman ONE.',
        'Выберите сервер и нажмите круглую кнопку питания на экране подключения.'),
    'happ': ('📱 **Как добавить подписку в Happ**',
        'Скопируйте ссылку Freeman ONE целиком.',
        'Откройте Happ и нажмите **+** в правом верхнем углу.',
        'Нажмите **«Вставить из буфера обмена»** и выберите «Авто».',
        'Нажмите круглую кнопку питания над списком серверов.'),
    'windows': ('💻 **Как настроить Happ на Windows**',
        'Откройте релизы Happ и скачайте установщик **.exe** из раздела **Assets**. Запустите его.',
        'Скопируйте ссылку подписки на компьютере из «Моих ключей», письма или переписки, где её выдали.',
        'Откройте Happ. В окне **«Вставить»** нажмите **Ctrl + V**, затем **«Поехали»**. Если окна нет, начните с кнопки **+**.',
        'Выберите сервер Freeman ONE и нажмите кнопку подключения.'),
    'windows_incy': ('💻 **Как настроить INCY на Windows**',
        'На странице загрузок выберите Windows и запустите установщик .exe. Это предварительная версия.',
        'Скопируйте ссылку подписки на компьютер. В INCY откройте добавление подписки и вставьте её.',
        'Выберите сервер Freeman ONE и нажмите кнопку подключения.'),
    'macos': ('💻 **Как настроить Happ на Mac**',
        'Скачайте **.dmg** из релизов Happ и перетащите приложение в папку **«Программы»**.',
        'На Mac скопируйте ссылку подписки из «Моих ключей», письма или переписки, где её выдали.',
        'Откройте Happ, нажмите **+** и добавьте подписку из буфера обмена.',
        'Выберите сервер и включите подключение. Если macOS попросит разрешение на добавление конфигурации VPN, подтвердите его.'),
    'tv': ('📺 **Как настроить INCY на Android TV**',
        'Установите INCY на телефон и добавьте в него ссылку Freeman ONE.',
        'Установите INCY на телевизор из Google Play.',
        'На ТВ откройте INCY, выберите **«+ Добавить»** и оставьте QR-код на экране.',
        'На телефоне откройте меню **⋮** рядом с подпиской и выберите **«Отправить на ТВ»**. Отсканируйте QR-код или введите код с экрана ТВ.',
        'После появления Freeman ONE выберите сервер на телевизоре и нажмите кнопку питания.'),
    'tv_happ': ('📺 **Как настроить Happ на Android TV**',
        'Установите Happ на телефон и добавьте подписку через «+» → «Вставить из буфера обмена».',
        'Скачайте APK, перенесите на телевизор и откройте его в файловом менеджере.',
        'Подключите телефон и телевизор к одной Wi-Fi-сети. Откройте QR-код в Happ на ТВ.',
        'Отсканируйте код сканером внутри Happ на телефоне, выберите Freeman ONE и подтвердите передачу.',
        'Если код не читается, на ТВ выберите «Импорт через сайт» (Web Import) и следуйте инструкции по кнопке ниже.',
        'Выберите сервер на ТВ и нажмите кнопку питания.'),
}

TELEGRAM_KEYS = ('🔑 Подписку, купленную в @FreemanOneBot, можно найти в разделе '
                 '**«Мои ключи»**. Там можно открыть нужную подписку и скопировать её ссылку.')


def connection_text(topic):
    title, *steps = CONNECTION_STEPS[topic]
    return '\n\n'.join([title, *(f'{index}. {step}' for index, step in enumerate(steps, 1))])

# These assets are composites of the coded diagrams on the public guide, not
# uploaded screenshots or customer media. Only server-selected IDs are used.
SETUP_TOPICS = (*CONNECTION_STEPS, 'android_happ', 'android_incy', 'android_rabbit')


def setup_media(topic):
    if not isinstance(topic, str) or topic not in SETUP_TOPICS:
        return None
    canonical = topic.removeprefix('android_')
    return 'setup-guide-' + canonical + '.png'


def setup_guide(topic):
    from .registry import URLS

    asset = setup_media(topic)
    if asset is None:
        raise ValueError('unknown_setup_guide')
    canonical = topic.removeprefix('android_')
    return dict(
        text=connection_text(canonical),
        actions=[
            dict(type='setup_image', material=topic),
            dict(type='link', label='Открыть инструкцию', url=URLS['setup_'+topic]),
            *([dict(type='link', label='Скачать в Google Play', url=URLS['rabbit_android'])]
              if topic == 'android_rabbit' else []),
        ],
    )


# Customer-facing fallbacks derived from SERVICE-06–13, WIN-01-01,
# MAC-BROWSER-01, NET-01-01 and DNS-YT-01. Regular model turns still use the KB.
_DIAGNOSIS = 'Диагностика: '
_DESKTOP = re.compile(r'\b(?:пк|windows|macos|desktop)\b|компьютер|ноутбук|виндовс|макбук')
_WINDOWS = re.compile(r'\bwindows\b|виндовс')
_MAC = re.compile(r'\bmacos\b|макбук|\bмак\b')
_NEXT = re.compile(r'^(?:нет\b|не помог|по-прежнему|без изменений|вс[её] так же|в обеих|на обеих)')
_PING_FAILED = re.compile(r'(?:ни один|все серверы|нигде|ничего).{0,60}(?:пинг|не отвеч|не пинг)|пинг.{0,30}(?:везде|нигде)')
_PROXY_OFF = re.compile(r'прокси.{0,24}(?:отключ|выключ)|(?:отключ|выключ)\w*\s+прокси')
_NETWORK_STEP = ('📶 **Попробуем другую сеть**\n\nСмените Wi‑Fi на мобильный интернет или наоборот '
                 'и повторите проверку. На другой сети тоже не работает?')
_SERVER_STEP = ('🌍 **Попробуйте другой сервер**\n\nОткройте приложение VPN, выберите другой сервер, '
                'у которого есть пинг, и повторите то же действие. Стало лучше?')
_REFRESH_STEP = ('🔄 **Обновите подписку**\n\nРядом с названием **FreemanOne** нажмите круглую стрелку '
                 'обновления, если она есть, и заново проверьте пинг. Появились цифры рядом с серверами?')


def _quick(text, pending='', actions=()):
    return QuickDiagnostic(text, _DIAGNOSIS + pending if pending else '', actions)


@dataclass(frozen=True)
class QuickDiagnostic:
    text: str
    expected_answer: str
    actions: tuple

    def remember(self, state):
        state.expected_answer = self.expected_answer
        if state.phase == 'clarification':
            state.phase = 'waiting_customer'


def _desktop_check(query, windows, mac):
    salutation = 'Ва алейкум салам! 🙂 ' if 'ассаламу алейкум' in query else ''
    platform = '' if windows or mac else 'На компьютере **Windows или macOS**?\n\n'
    return _quick(
        salutation + 'Давайте разберёмся с VPN на компьютере.\n\n' + platform
        + 'Отключите VPN и попробуйте открыть обычный сайт. Интернет без VPN работает?',
        'интернет без VPN на Windows' if windows else
        'интернет без VPN на macOS' if mac else 'Windows или macOS и интернет без VPN',
    )


def _network_speed():
    from .registry import URLS

    return _quick(
        '📊 **Проверим скорость обычного интернета**\n\n'
        '1️⃣ Отключите VPN.\n\n'
        '2️⃣ Откройте Интернетометр по кнопке ниже и нажмите **«Измерить»**.\n\n'
        '3️⃣ Пришлите результат, где видны входящая и исходящая скорость.',
        'результат замера скорости без VPN',
        (dict(type='link', label='Проверить скорость', url=URLS['speed']),),
    )


def quick_diagnostic(state, query, context='', messenger=None, has_recent_image=False, *, reported_steps=False):
    from .intents import first_vpn_diagnostic, messenger_issue, technical_issue

    latest = query.casefold().strip(' .!?')
    pending = state.expected_answer.removeprefix(_DIAGNOSIS) if state.expected_answer.startswith(_DIAGNOSIS) else ''
    combined = ' '.join((latest, context.casefold()))
    short_feedback = len(latest.split()) <= 9
    failed = bool(short_feedback and _NEXT.search(latest))
    messenger = messenger or messenger_issue(query) or messenger_issue(context)
    screenshot_request = latest == 'посмотрите скриншот и помогите разобраться'
    windows = bool(_WINDOWS.search(combined) or 'windows' in pending.casefold())
    mac = bool(_MAC.search(combined) or 'macos' in pending.casefold())

    # A new, specific complaint takes precedence over a pending yes/no check.
    if pending and short_feedback:
        if re.fullmatch(r'(?:вс[её] )?(?:заработало|получилось|теперь работает)(?:,? спасибо)?', latest):
            return _quick('Отлично, что заработало! 🙌')
        if pending.startswith(('интернет без VPN на', 'Windows или macOS и')):
            if re.search(r'(?:без (?:vpn|впн).{0,22}работает|^да\b)', latest) and not re.search(r'не\s+работает', latest):
                return _quick(_SERVER_STEP, 'результат проверки другого сервера')
            if failed or re.search(r'без (?:vpn|впн).{0,22}не работает', latest):
                if windows:
                    return _quick(
                        '⚙️ **Проверим прокси в Windows**\n\n'
                        'Откройте **Пуск**, найдите **«Настройки прокси-сервера»** и откройте этот раздел. '
                        'Проверьте, включён ли **«Использовать прокси-сервер»**. Если компьютер рабочий, '
                        'оставьте настройки как есть. Напишите, включён ли прокси.',
                        'состояние системного прокси Windows',
                    )
                return _quick('Попробуйте открыть тот же сайт в другом браузере. Он там открывается?',
                              'результат проверки в другом браузере')
            if (windows or mac) and re.fullmatch(r'(?:у меня |на )?(?:windows(?: 1[01])?|виндовс|macos|мак|макбук)', latest):
                return _desktop_check(latest, windows, mac)
        if failed and pending == 'пинг после смены сети':
            return _quick(_REFRESH_STEP, 'пинг после обновления подписки')
        if failed and pending == 'пинг после обновления подписки':
            return _network_speed()
        if failed and pending.startswith('результат проверки сети для '):
            return _quick(_SERVER_STEP, 'результат проверки другого сервера')
        if failed and pending == 'результат проверки другого сервера':
            return _quick(
                '🔄 Перезагрузите устройство, включите VPN и повторите то же действие. Получилось?',
                'результат после перезагрузки',
            )
        if failed and pending == 'результат после перезагрузки':
            return _quick('Уточните, какое приложение VPN вы используете и появляется ли текст ошибки?',
                          'название приложения и ошибка после перезагрузки')
        if failed and pending == 'результат повторного запуска ролика YouTube':
            return _quick('Откройте ещё один ролик на YouTube. Ошибка появляется на всех видео или только на одном?',
                          'одно или все видео YouTube')
        if not messenger and pending.startswith('результат проверки встроенного прокси '):
            messenger = pending.rsplit(' ', 1)[-1]
    if messenger:
        if failed or _PROXY_OFF.search(combined):
            return _quick(_NETWORK_STEP, 'результат проверки сети для ' + messenger)
        if _DESKTOP.search(combined):
            return _quick(f'Что именно сбоит в {messenger} на компьютере: сообщения, звонки или медиа?')
        if not reported_steps:
            label = 'Прокси-сервер' if messenger == 'WhatsApp' else 'Прокси'
            return _quick(
                f'🔎 **Проверим {messenger}**\n\n'
                f'На телефоне откройте **Настройки → Данные и память → {label}**. Если прокси включён, '
                'выключите его и повторите действие. Если VPN включён, оставьте его включённым.',
                'результат проверки встроенного прокси ' + messenger,
            )
        return _quick(_NETWORK_STEP, 'результат проверки сети для ' + messenger)
    if re.search(r'youtube|ютуб', combined) and 'контент недоступен' in combined:
        return _quick(
            '📺 **YouTube пишет «Контент недоступен»**\n\n'
            'Войдите в аккаунт YouTube и снова откройте тот же ролик. '
            'Если вы уже вошли, выйдите из аккаунта и войдите повторно. Получилось?',
            'результат повторного запуска ролика YouTube',
        )
    if not (technical_issue(query) or technical_issue(context) or _PING_FAILED.search(combined) or screenshot_request):
        return None
    if re.search(r'gemini|джемини|chatgpt|claude', combined) and not re.search(r'\bсша\b|\busa\b', combined):
        return _quick(
            '🌐 Выберите в приложении VPN сервер **США** и снова откройте сервис. '
            'Если появится ошибка, напишите, что там сказано.',
            'результат проверки сервиса на сервере США',
        )
    if _PING_FAILED.search(combined):
        if re.search(r'в обеих сетях|на обеих сетях', latest):
            return _quick(_REFRESH_STEP, 'пинг после обновления подписки')
        return _quick(_NETWORK_STEP, 'пинг после смены сети')
    if _DESKTOP.search(combined):
        return _desktop_check(latest, windows, mac)
    if has_recent_image or screenshot_request:
        return _quick(
            '📸 Скриншот получил. Подскажите, сбой только в одном приложении или через VPN ничего не открывается?',
            'одно приложение или общее подключение VPN',
        )
    return _quick(first_vpn_diagnostic(query) or (
        '🔎 Давайте разберёмся. Что сейчас видно на экране? Если есть ошибка, пришлите её текст или скриншот.'
    ), 'экран ошибки и что именно не работает')

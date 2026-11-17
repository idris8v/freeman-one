EDITABLE_BUTTONS = {
    'cabinet':'👤 Личный кабинет на сайте',
    'plans':'💳 Купить подписку',
    'trial': '🎁 Пробный период', 'buy': '💳 Купить подписку',
    'subscriptions': '🔑 Мои ключи', 'balance': '💰 Баланс',
    'referral': '👥 Хочу заработать', 'help': '📖 Помощь',
    'activate': '🎁 Активировать пробный период', 'pay': '💳 Оплатить СБП',
    'promo': '🎟 Ввести промокод', 'connection': '🔑 Подключение',
    'devices': '📱 Устройства', 'renew': '🔄 Продлить', 'history': '🧾 История',
    'device_add': '➕ Добавить устройство', 'deposit': '➕ Пополнить баланс',
    'ledger': '📜 История операций', 'referral_history': '📜 История приглашений',
    'instructions': '📱 Как подключиться', 'faq': '❓ Частые вопросы',
    'support': '💬 Поддержка', 'documents': '📄 Документы',
    'rename': '✏️ Назвать подписку',
    'keys': '🔑 Мои ключи',
    'withdraw': '💸 Вывести средства', 'withdrawals': '🧾 Заявки',
    'notifications': '🔔 Напоминания',
    'proxy': '🌐 Telegram Proxy', 'gifts': '🎁 Подарить подписку',
    'install_subscription': '📲 Установить подписку',
    'copy_key': '🔗 Скопировать ключ',
    'download_config': '📄 Скачать конфигурацию',
    'detailed_instructions': '📖 Подробные инструкции',
    'video_instruction': '🎬 Видеоинструкция',
    'autorenew': '🔄 Автопродление',
    'subscription_extra': '⚙️ Дополнительно',
    'gift_subscription': '🎁 Подарить другу',
    'delete_subscription': '🗑 Удалить подписку и отозвать ключ',
}

SETTING_FIELDS = {
    'trial_hours': ('Срок пробного периода в часах (24 или 168 для 7 дней)', 1, 8760, '24'),
    'device_limit': ('Лимит устройств для новых пробных подписок', 1, 100, '1'),
    'referral_percent': ('Процент вознаграждения с подтверждённых покупок', 0, 100, '10'),
    'referral_min_purchase': ('Минимальная покупка для бонуса, в рублях', 0, 1000000, '0'),
}

# Screens where an owner can place a custom client button.  The stored value is
# deliberately stable while the Russian title can change without a migration.
CUSTOM_BUTTON_LOCATIONS = {
    'home': 'Главное меню',
    'buy': 'Покупка подписки',
    'subscriptions': 'Мои подписки',
    'balance': 'Баланс',
    'help': 'Помощь',
    'instructions': 'Инструкции',
    'documents': 'Документы',
    'proxy': 'Telegram-прокси',
}

# A custom button may open one of these existing client sections or a public
# HTTPS address.  Restricting callbacks prevents access to admin-only actions.
CUSTOM_BUTTON_TARGETS = {
    'home', 'buy', 'subscriptions', 'balance', 'help', 'instructions',
    'documents', 'faq', 'support', 'referral', 'p:home', 'g:home', 'v:home',
}


EDITABLE_BUTTONS = {'cabinet': '👤 Личный кабинет на сайте',
 'plans': '💳 Купить подписку',
 'trial': '🎁 Пробный период',
 'buy': '💳 Купить подписку',
 'subscriptions': '🔑 Мои ключи',
 'balance': '💰 Баланс',
 'referral': '👥 Хочу заработать',
 'help': '📖 Помощь',
 'activate': '🎁 Активировать пробный период',
 'pay': '💳 Оплатить СБП',
 'promo': '🎟 Ввести промокод',
 'connection': '🔑 Подключение',
 'devices': '📱 Подключённые устройства',
 'renew': '🔄 Продлить',
 'history': '🧾 История',
 'device_add': '➕ Добавить устройство',
 'deposit': '➕ Пополнить баланс',
 'ledger': '📜 История операций',
 'referral_history': '📜 История приглашений',
 'instructions': '📱 Как подключиться',
 'faq': '❓ Частые вопросы',
 'support': '💬 Поддержка',
 'documents': '📄 Документы',
 'rename': '✏️ Имя ключа',
 'keys': '🔑 Мои ключи',
 'withdraw': '💸 Вывести средства',
 'withdrawals': '🧾 Заявки',
 'notifications': '🔔 Напоминания',
 'proxy': '🌐 Telegram Proxy',
 'gifts': '🎁 Подарить подписку',
 'install_subscription': '📲 Установить подписку',
 'copy_key': '📋 Скопировать ключ',
 'download_config': '📄 Скачать конфигурацию',
 'detailed_instructions': '📖 Подробные инструкции',
 'video_instruction': '🎬 Видеоинструкция',
 'autorenew': '🔄 Автопродление',
 'subscription_extra': '⚙️ Дополнительно',
 'gift_subscription': '🎁 Подарить другу',
 'delete_subscription': '🗑 Удалить подписку и отозвать ключ',
 'daily': 'Настроить ежедневную оплату',
 'sub': '📦 Подписка',
 'sub_apply': '🗑 Да, удалить и отозвать ключ',
 'general_cancel': 'Отмена',
 'key_apply': '✅ Подтвердить',
 'notifytoggle': 'Включить / выключить',
 'general_back': '← Назад',
 'input': '✍️ Указать реквизиты',
 'withdrawapply': '✅ Подать заявку',
 'url_c706a11dd67b': '💬 Контакт',
 'gift_input': 'Получатель подарка',
 'gift_list': '🗂 Мои подарки',
 'gift_confirm': '✅ Подтвердить подарок',
 'gift_choose': '✍️ Ввести username или ID',
 'gift_item': 'Подарок в списке',
 'gift_cancel': '✅ Отменить подарок',
 'proxy_open': 'Старая ссылка · 1',
 'url_a739606ed00f': '💳 Оплатить картой / СБП',
 'proxy_cardcheck': '🔄 Проверить оплату',
 'url_a900ef0a331f': '🔌 Подключить в Telegram',
 'proxy_buyapply': '✅ Подтвердить оплату',
 'url_36ddb4d3cfcb': '💬',
 'url_04d58d34267b': '💬 Связаться с клиентом',
 'proxy_buy': '💰 Оплатить с баланса',
 'proxy_card': '💳 Попробовать снова',
 'url_ec98c63733dd': '🔌 1 · 1',
 'url_3fdb9b7ab779': 'Подключить Telegram Proxy',
 'url_d4547a10eeaf': '👤 Личный кабинет',
 'tariff': 'Тариф в списке',
 'pay_balance': '💰 Оплатить с баланса',
 'keydetail': 'Ключ в списке',
 'tg_approve': 'Подтвердить вход',
 'url_32bceab8e70a': 'Открыть личный кабинет',
 'stars': '⭐ Оплатить Telegram Stars · ',
 'url_a903a9d45f30': '📤 Поделиться',
 'url_b83febd2481b': '💬 Написать в поддержку',
 'rotate_confirm': '🔐 Перевыпустить ключ',
 'demo_download': '📄 Скачать тестовый ключ',
 'device_rotate': 'Отвязать все устройства',
 'device_rotate_apply': 'Да, отвязать все',
 'key_extra': '⚙️ Дополнительно к ключу',
 'vpn_connection': '📲 Установить подписку',
 'list': '🗂 Мои обращения',
 'topup_pay': '💳 Перейти к оплате',
 'url_32d8b10b3fd9': '💳 Оплатить в ЮKassa / СБП',
 'topup_check': '🔄 Проверить оплату',
 'order_check': '🔄 Проверить оплату',
 'device_list': 'Следующие →',
 'vpn_download': '📄 Скачать конфигурацию',
 'vpn_locations': '🌍 Сменить сервер',
 'vpn_select': 'Выбор VPN-сервера',
 'vpn_link': '🔗 Показать ключ',
 'vpn_qr': 'QR →',
 'device_view': 'Устройство в списке',
 'device_confirm': 'Отключить устройство',
 'device_apply': 'Да, отключить',
 'rotate_apply': 'Да, перевыпустить',
 'devices_back': '← Назад',
 'devices_cancel': 'Отмена',
 'keys_back': '← Назад',
 'keys_cancel': 'Отмена',
 'proxy_back': '← Назад',
 'proxy_cancel': 'Отмена',
 'gifts_back': '← Назад',
 'gifts_cancel': 'Отмена',
 'url_b84eba89c1ec': '📲 Подключить',
 'url_8463a7274c7b': '📲 Открыть подписку',
 'copy_freeman_id': 'Скопировать Freeman ID',
 'copy_referral': '📋 Скопировать реферальную ссылку'}

BUTTON_GROUPS = {'keys': ('🔑 Мои ключи',
          ['subscriptions',
           'connection',
           'renew',
           'history',
           'rename',
           'keys',
           'install_subscription',
           'copy_key',
           'download_config',
           'autorenew',
           'subscription_extra',
           'gift_subscription',
           'delete_subscription',
           'sub',
           'sub_apply',
           'key_apply',
           'rotate_confirm',
           'key_extra',
           'vpn_connection',
           'vpn_download',
           'vpn_locations',
           'vpn_select',
           'vpn_link',
           'vpn_qr',
           'rotate_apply',
           'keys_back',
           'keys_cancel',
           'url_b84eba89c1ec',
           'url_8463a7274c7b',
           'keydetail']),
 'devices': ('📱 Подключённые устройства',
             ['devices',
              'device_add',
              'device_rotate',
              'device_rotate_apply',
              'device_list',
              'device_view',
              'device_confirm',
              'device_apply',
              'devices_back',
              'devices_cancel']),
 'proxy': ('🌐 Telegram Proxy',
           ['proxy',
            'proxy_open',
            'proxy_cardcheck',
            'proxy_buyapply',
            'proxy_buy',
            'proxy_card',
            'proxy_back',
            'proxy_cancel',
            'url_a900ef0a331f',
            'url_3fdb9b7ab779']),
 'gifts': ('🎁 Подарки',
           ['gifts',
            'gift_input',
            'gift_list',
            'gift_confirm',
            'gift_choose',
            'gift_item',
            'gift_cancel',
            'gifts_back',
            'gifts_cancel']),
 'payments': ('💳 Покупка и баланс',
              ['plans',
               'trial',
               'buy',
               'balance',
               'activate',
               'pay',
               'promo',
               'deposit',
               'ledger',
               'withdraw',
               'withdrawals',
               'daily',
               'withdrawapply',
               'pay_balance',
               'topup_pay',
               'topup_check',
               'order_check',
               'tariff',
               'stars',
               'input',
               'url_a739606ed00f',
               'url_32d8b10b3fd9']),
 'referral': ('👥 Заработок',
              ['referral',
               'referral_history',
               'url_a903a9d45f30',
               'copy_referral']),
 'support': ('💬 Поддержка и помощь',
             ['help',
              'faq',
              'support',
              'documents',
              'notifications',
              'detailed_instructions',
              'video_instruction',
              'list',
              'notifytoggle',
              'url_c706a11dd67b',
              'url_04d58d34267b',
              'url_b83febd2481b']),
 'menu': ('🏠 Меню и переходы',
          ['cabinet',
           'instructions',
           'general_cancel',
           'general_back',
           'url_36ddb4d3cfcb',
           'url_ec98c63733dd',
           'url_d4547a10eeaf',
           'url_32bceab8e70a',
           'demo_download',
           'copy_freeman_id',
           'tg_approve'])}

def stable_button_key(target, action='', label=''):
    if target.startswith('cg:'):return 'customer_gift_'+target.split(':')[1]
    if target.startswith(('admin:giftlink:','admin:input:giftlink:')):
        op=target.split(':')[2] if target.startswith('admin:giftlink:') else target.split(':')[3]
        return 'gift_link_'+op
    if target.startswith('url:https://t.me/share/url?') and action=='gift_links':return 'gift_link_share'
    if target.startswith('url:') and '?start=fg_' in target:return 'gift_link_get'
    if target.startswith('admin:grantconfig:new:'):return 'admin_grant_custom'
    if target.startswith('checkout:'):return 'checkout_continue'
    if target.startswith('checkout_balance:'):return 'pay_balance'
    if target.startswith('checkout_check:'):return 'order_check'
    if target.startswith(('buy_devices:','renew_devices:','catalog_groups:')):return 'catalog_devices'
    if target.startswith('catalog_table:'):return 'catalog_table'
    import hashlib
    if label.startswith('←') or label in ('Отмена','Отменить'):
        section='devices' if action.startswith(('vd:','devices:','device_rotate')) else 'keys' if action.startswith(('vr:','sub:','keydetail:','subscription_extra:','rename:','renew:')) else 'proxy' if action.startswith('p:') else 'gifts' if action.startswith('g:') else 'general'
        return section+('_cancel' if label in ('Отмена','Отменить') or 'Отмен' in label else '_back')
    if target.startswith(('url:','webapp:')) and '/auth/bot/support?' in target:
        return 'support_staff' if 'mode=staff' in target else 'support_new' if 'mode=new' in target else 'support_list'
    if target=='t:input:new':return 'support_new'
    if target.startswith('t:list:'):return 'support_list'
    if target.startswith('p:access:'):return 'proxy_access'
    if target=='p:purchase':return 'proxy_purchase'
    if target.startswith(('url:tg://proxy','url:https://t.me/proxy')):return 'proxy_connect'
    if target.startswith('custom:'):return target.replace(':','_',1)
    if target.startswith(('key_control:delete:','sub_control:delete:')):return 'delete_subscription'
    if target.startswith('demo_download:') and target.endswith(':config'):return 'download_config'
    if target.startswith('copy:'):
        return 'copy_referral' if action.startswith('referral') else 'copy_key'
    aliases={'subscription_extra':'subscription_extra','video_instruction':'video_instruction','instructions':'detailed_instructions','install':'install_subscription','autorenew':'autorenew'}
    first=target.split(':')[0]
    if first in aliases:return aliases[first]
    if target.startswith('g:input:subscription:'):return 'gift_subscription'
    if target.startswith('url:'):
        if '/auth/bot?' in target:return 'cabinet'
        if 'Поделиться' in label:return 'referral_share'
        if any(word in label.casefold() for word in ('юkassa','юкасс','оплатить карт','карта / сбп','перейти к оплате','оплатить сбп')):return 'payment_checkout'
        return 'url_'+hashlib.sha256(label.encode()).hexdigest()[:12]
    if target=='p:home':return 'proxy'
    if target=='g:home':return 'gifts'
    if first in ('p','g','v','vd','vr'):
        return {'p':'proxy','g':'gift','v':'vpn','vd':'device','vr':'rotate'}[first]+'_'+target.split(':')[1]
    if first in ('x','t'):return target.split(':')[1]
    return first

EDITABLE_BUTTONS['admin_grant_custom']='⚙️ Свой срок и устройства'
BUTTON_GROUPS['payments'][1].append('admin_grant_custom')

def button_group(key):
    return next((name for name,(_,keys) in BUTTON_GROUPS.items() if key in keys),'menu')

# The primary client interface is VLESS; old Amnezia/demo choices are not advertised.
_LEGACY_BUTTONS={'device_add','download_config','demo_download','install_subscription','instructions','detailed_instructions','video_instruction','connection','url_8463a7274c7b'}
_LEGACY_BUTTONS.update(key for key in EDITABLE_BUTTONS if key.startswith('vpn_'))
for _key in _LEGACY_BUTTONS:EDITABLE_BUTTONS.pop(_key,None)
for _name,(_title,_keys) in BUTTON_GROUPS.items():
    BUTTON_GROUPS[_name]=(_title,[key for key in _keys if key in EDITABLE_BUTTONS])

def without_leading_emoji(label):
    import re
    # Preserve numbering, but replace a leading emoji sequence including flags/ZWJ.
    return re.sub(r'^(\d+\.\s*)?[\U0001F000-\U0001FAFF\u2600-\u27BF\u200d\ufe0f\ufe0e]+\s*',lambda m:m.group(1) or '',label).strip()

MEDIA_PAGES={
 'home':'Главное меню','plans':'Выбор покупки','buy':'Тарифы','tariff':'Выбранный тариф','trial':'Пробная подписка','activate':'Активация',
 'subscriptions':'Мои ключи','keys':'Список ключей','key_card':'Карточка ключа и QR-код','receipt':'После оплаты и новый ключ',
 'subscription_extra':'Дополнительно к ключу','rename':'Имя ключа','renew':'Продление','autorenew':'Автопродление','history':'История подписки',
 'devices':'Список подключённых устройств','device_card':'Карточка устройства','device_confirm':'Отключение устройства: подтверждение',
 'device_result':'Отключение устройства: результат','device_rotate':'Отвязка всех устройств: подтверждение','device_rotate_apply':'Отвязка всех устройств: результат',
 'rotate_confirm':'Перевыпуск ключа: подтверждение','rotate_result':'Новый ключ после перевыпуска',
 'delete_confirm':'Удаление подписки: подтверждение','delete_result':'Удаление подписки: результат',
 'proxy':'Telegram Proxy: список','proxy_card':'Карточка Telegram Proxy','proxy_buy':'Покупка Telegram Proxy','proxy_pay':'Оплата Telegram Proxy',
 'gifts':'Подарки','gift_card':'Карточка подарка','gift_confirm':'Подтверждение подарка',
 'balance':'Баланс','deposit':'Пополнение','topup_pay':'Оплата пополнения','topup_check':'Проверка пополнения',
 'ledger':'История баланса','withdraw':'Вывод средств','withdrawals':'Заявки на вывод','withdrawconfirm':'Подтверждение вывода',
 'referral':'Заработок','referral_history':'История приглашений','help':'Помощь','instructions':'Инструкция VLESS',
 'faq':'Частые вопросы','answer':'Ответ FAQ','support':'Поддержка','ticket_card':'Обращение в поддержку',
 'documents':'Документы','document':'Текст документа','promo':'Промокод','notifications':'Уведомления','pay':'Оплата подписки',
}

def appearance_page(action):
    p=action.split(':');first=p[0]
    if first in ('checkout','checkout_view','checkout_check','checkout_balance'):return 'pay'
    if first in ('sub','keydetail','connection','install'):return 'key_card'
    if first=='vd':return {'list':'devices','view':'device_card','confirm':'device_confirm','apply':'device_result'}.get(p[1],'devices')
    if first=='vr':return 'rotate_confirm' if p[1]=='confirm' else 'rotate_result'
    if first in ('sub_control','key_control'):return 'delete_confirm'
    if first in ('sub_apply','key_apply'):return 'delete_result'
    if first in ('payment_master','payment_card'):return 'payment_card' if first=='payment_card' else 'payment_management'
    if first=='p':return {'home':'proxy','open':'proxy_card','access':'proxy_card','purchase':'proxy_buy','buy':'proxy_buy','buyapply':'proxy_pay','card':'proxy_pay','cardcheck':'proxy_pay'}.get(p[1],'proxy')
    if first=='g':return {'item':'gift_card','confirm':'gift_confirm'}.get(p[1],'gifts')
    if first=='t':return 'support' if p[1] in ('home','new','list') else 'ticket_card'
    if first=='x':return p[1]
    return first

VLESS_INSTRUCTIONS={
 'vless_ios':('🍎 iPhone / iPad','Установите Happ или v2RayTun.\n\nОткройте «Мои ключи» и выберите подписку. Нажмите «Подключить» либо «Скопировать ключ».\n\nВ приложении добавьте подписку по ссылке или импортируйте её из буфера обмена. Также можно отсканировать QR-код.\n\nРазрешите создание VPN-подключения и включите его. При перевыпуске ключа замените старую подписку новой.'),
 'vless_android':('🤖 Android','Установите Happ или v2RayTun.\n\nВ «Моих ключах» откройте подписку и нажмите «Подключить» или «Скопировать ключ».\n\nВ приложении импортируйте ссылку из буфера обмена либо отсканируйте QR-код. Разрешите VPN-подключение и включите его.\n\nНе передавайте ссылку другим людям. После перевыпуска используйте новый ключ.'),
 'vless_desktop':('💻 Компьютер','Установите приложение с поддержкой VLESS и ссылок подписки.\n\nСкопируйте ключ из карточки подписки. В приложении добавьте подписку по ссылке или импортируйте из буфера обмена. Обновите список серверов и включите подключение.\n\nЕсли возникла ошибка, откройте «Дополнительно → Поддержка» в карточке ключа.'),
}
EDITABLE_BUTTONS['instructions']='📖 Инструкция VLESS'
EDITABLE_BUTTONS['instruction']='Инструкция устройства'
BUTTON_GROUPS['support'][1].extend(['instructions','instruction'])
MEDIA_PAGES['instruction']='Инструкция устройства VLESS'
MEDIA_GROUPS={
 'menu':('🏠 Меню и покупки',['home','plans','buy','tariff','trial','activate','pay','promo']),
 'keys':('🔑 Мои ключи',['subscriptions','keys','key_card','receipt','subscription_extra','rename','renew','autorenew','history','rotate_confirm','rotate_result','delete_confirm','delete_result']),
 'devices':('📱 Устройства',['devices','device_card','device_confirm','device_result','device_rotate','device_rotate_apply']),
 'proxy':('🌐 Telegram Proxy',['proxy','proxy_card','proxy_buy','proxy_pay']),
 'gifts':('🎁 Подарки',['gifts','gift_card','gift_confirm']),
 'balance':('💰 Баланс и заработок',['balance','deposit','topup_pay','topup_check','ledger','withdraw','withdrawals','withdrawconfirm','referral','referral_history']),
 'help':('💬 Помощь и поддержка',['help','instructions','instruction','faq','answer','support','ticket_card','documents','document','notifications']),
}

# Current client screens and stable button keys. Keep old keys for saved settings.
_CURRENT_GROUPS={
 'payments':{'payment_management':'💳 Автоплатежи','payment_subscriptions':'🔑 Подписки и автопродление','payment_master':'⏸ Управление автоплатежами','payment_card':'💳 Сохранённая карта','card_bind_info':'➕ Привязать карту через ЮKassa'},
 'referral':{'referral_details':'💰 Сколько можно заработать'},
 'proxy':{'proxy_purchase':'🛒 Купить прокси','proxy_access':'🌐 Прокси в списке','proxy_connect':'✈️ Подключить прокси'},
 'support':{'support_new':'💬 Написать в поддержку','support_list':'🗂 Мои обращения','support_staff':'🗂 Обращения','document':'📄 Документ'},
}
for group,buttons in _CURRENT_GROUPS.items():
 EDITABLE_BUTTONS.update(buttons)
 for key in buttons:
  if key not in BUTTON_GROUPS[group][1]:BUTTON_GROUPS[group][1].append(key)
MEDIA_PAGES.update({'referral_details':'Сколько можно заработать','payment_management':'Автоплатежи и карты','payment_subscriptions':'Автопродление подписок','payment_card':'Сохранённая карта','card_bind_info':'Привязка карты через ЮKassa'})
MEDIA_GROUPS['balance'][1].append('referral_details')
MEDIA_GROUPS['payments']=('💳 Автоплатежи',['payment_management','payment_subscriptions','payment_card','card_bind_info','autorenew'])

# Remove obsolete controls from the editor only; existing stored settings remain intact.
_RETIRED={'stars','copy_freeman_id','copy_referral','notifications','notifytoggle','proxy_open','connection','install_subscription','download_config','demo_download','video_instruction','detailed_instructions','instructions','instruction','general_cancel','input','key_apply','daily','sub_apply'}
_RETIRED.update(k for k in EDITABLE_BUTTONS if k.startswith('url_'))
for key in _RETIRED:EDITABLE_BUTTONS.pop(key,None)
for group,(title,keys) in BUTTON_GROUPS.items():BUTTON_GROUPS[group]=(title,[k for k in keys if k in EDITABLE_BUTTONS])
for group,key,title in [('referral','referral_share','📤 Поделиться'),('payments','payment_checkout','💳 Оплатить СБП'),('payments','checkout_continue','💳 Перейти к оплате')]:
 EDITABLE_BUTTONS[key]=title
 if key not in BUTTON_GROUPS[group][1]:BUTTON_GROUPS[group][1].append(key)
_RETIRED_SCREENS={'device_rotate','device_rotate_apply','instructions','instruction','notifications'}
for key in _RETIRED_SCREENS:MEDIA_PAGES.pop(key,None)
for group,(title,pages) in MEDIA_GROUPS.items():MEDIA_GROUPS[group]=(title,[p for p in pages if p in MEDIA_PAGES])

# Device-first catalogue controls use the existing appearance editor.
EDITABLE_BUTTONS.update(catalog_devices='рџ“± РљРѕР»РёС‡РµСЃС‚РІРѕ СѓСЃС‚СЂРѕР№СЃС‚РІ',catalog_table='рџ“Љ Р’СЃРµ С‚Р°СЂРёС„С‹')
BUTTON_GROUPS['payments'][1].extend(['catalog_devices','catalog_table'])

# Gift link controls use the existing appearance editor.
for _k,_v in {'home':'Подарки по ссылке','vpn':'Подарить VPN','proxy':'Подарить Telegram Proxy','make':'Создать подарок','share':'Отправить другу','post':'Готовое сообщение','get':'Получить подарок','cancel':'Удалить приглашение','cancelok':'Подтвердить удаление','remove':'Удалить подарок из списка','removeok':'Подтвердить удаление из списка'}.items():
    EDITABLE_BUTTONS['gift_link_'+_k]=_v
    BUTTON_GROUPS.setdefault('gift_links',('🎁 Подарки по ссылке',[]))[1].append('gift_link_'+_k)

for _k,_v in {'hub':'Подарить другу','home':'Купить подарок','list':'Мои подарки','balance':'Оплатить подарок с баланса','invoice':'Оплатить подарок через ЮKassa','rotate':'Перевыпустить ссылку'}.items():
    EDITABLE_BUTTONS['customer_gift_'+_k]=_v
    BUTTON_GROUPS.setdefault('customer_gifts',('🎁 Покупка подарка',[]))[1].append('customer_gift_'+_k)

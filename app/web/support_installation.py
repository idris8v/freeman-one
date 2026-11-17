"""Approved installation materials and a fenced, key-scoped device reset."""
import json
import re
from datetime import timedelta

from sqlalchemy import select

from app.database.models import AIConversation, AIKnowledge, AIMessage, AdminAudit, AppSetting, Subscription, VlessCredential, RemnawaveServer, WebQuickReply, now
from app.database.repositories import SQLRepository
from app.services.customer_identity import lock_nonce
from app.services.freeman_ai.security import encrypt, decrypt, aware
from app.web.support_rich import guide_html

INSTALL = '''🔑 **Как добавить подписку**

1️⃣ Скопируйте ссылку подписки, которую получили после покупки.

2️⃣ Откройте Happ или INCY и нажмите ➕.

3️⃣ Выберите «Добавить из буфера обмена». Подождите, пока появится список серверов.

4️⃣ Выберите «Авто», если есть такой пункт, или доступный сервер. Нажмите кнопку подключения.

Готово 🙂 Если на каком-то шаге застряли, напишите, что видите на экране.'''
REQUEST = '''🔑 Подписка, или ключ, — это ссылка, которую вы получили после оплаты. Она нужна, чтобы добавить подключение в приложение.

Выглядит примерно так
https://sub.freemanone.top/ваш-код

Пришлите вашу ссылку сюда — помогу с подключением.'''
TRANSFERRED = '''✅ Старые подключения отвязаны. Теперь добавьте подписку в новое приложение.

Не включайте её в старом приложении — оно снова займёт подключение. Ссылку подписки менять не нужно.'''
STYLE = '''Объясняй как человеку, который впервые видит VPN. Короткие предложения, знакомые слова, одно действие за шаг. Не используй «импорт конфигурации», «HWID», «слот» без простого объяснения. Отделяй шаги пустой строкой. Будь дружелюбным, терпеливым, поддерживай без снисхождения. Лёгкая доброжелательная шутка уместна изредка, когда нет сбоя, потери денег или раздражения клиента. Не шути над человеком. Не выдумывай, что ты человек. Не начинай каждый ответ приветствием и не повторяй уже выясненные вопросы.'''
KNOWLEDGE = '''Цель — помочь добавить уже купленную подписку в Happ или INCY.

Помоги по текущему запросу. Не начинай с выбора «первая установка или перенос». Если клиент просит добавить ключ, объясни добавление в его приложение. Только если клиент сам сказал, что меняет приложение или телефон, используй сценарий смены. Не предлагай смену или переустановку приложения по собственной инициативе. Название приложения уточни только когда без него нельзя объяснить следующий шаг.

Подписка, или ключ — ссылка после оплаты, например https://sub.freemanone.top/ваш-код. Пример не является рабочим ключом. Если обращение связано с подтверждённым аккаунтом сайта или Telegram, используй доступный инструмент подписок. При нескольких подписках помоги выбрать. Не утверждай, что видишь аккаунт, если такой связи нет. При ручной покупке в WhatsApp или в личной переписке запроси ссылку.

Первое подключение — отправь инструкцию без отвязки устройств.

Смена приложения/телефона — объясни, что старое приложение может занимать подключение. После получения нашей действующей ссылки и явного намерения переноса инструмент может автоматически отвязать старые подключения этой подписки без второго вопроса. Он отключает ВСЕ её устройства, поэтому заранее объясни это при запросе ссылки. Только после результата done говори об успехе. При иной ссылке, неизвестной подписке, ошибке либо отсутствии инструмента передай специалисту. Никогда не имитируй успешную отвязку текстом. Не включать подписку в старом приложении после переноса — оно снова займёт подключение. Срок и сама ссылка остаются прежними.

''' + INSTALL
PREFIX = 'support_install_20261110:'
URL = re.compile(r'https://sub\.freemanone\.top/[A-Za-z0-9_-]{6,200}(?=$|[\s<>)\]])')
TRANSFER = re.compile(r'\b(?:меняю|сменил[аи]?|поменял[аи]?|заменил[аи]?|сменить|поменять|перенести|переношу|перейти|(?:установить|устанавливаю|установил[аи]?)\s+(?:новое|другое))\b.{0,65}\b(?:приложени[ея]|телефон[ае]?|устройств[оа])\b', re.I)
LIMIT = re.compile(r'\bпревышен[оаы]?\s+(?:допустимое\s+)?(?:количество|число|лимит)\s+(?:подключений|устройств)|\bлимит\s+(?:подключений|устройств)\s+превышен\b',re.I)
NEGATIVE = re.compile(r'\bне\s+(?:хочу|планирую|собираюсь)\b.{0,65}\b(?:приложени[ея]|телефон[ае]?|устройств[оа])\b|\bне\s+превыш|\bне\s+(?:меняю|менял[аи]?|поменял[аи]?|переношу)|\bвпервые\b|\bперв(?:ое|ый|ый раз)\s+подключ', re.I)


def install_material(kind, mid):
    text = {'key_install': INSTALL, 'key_request': REQUEST, 'key_transfer_request': REQUEST+'\n\nПри переносе отвяжем все устройства этой подписки, чтобы освободить подключения.', 'key_transferred': TRANSFERRED}[kind]
    markup = guide_html(text, {}, False)
    if kind != 'key_install':
        markup += '<tg-button-row><tg-button type="callback_data" data="fo:install:' + str(mid) + '">Как добавить подписку</tg-button></tg-button-row>'
    return {'text': text, 'html': markup}


async def seed(session):
    repo = SQLRepository(session)
    await lock_nonce(session, PREFIX+'seed')
    async def save(model, key, title, text, existing=None):
        raw=await repo.setting(PREFIX+key) or (await repo.setting(existing) if existing else None)
        row=await session.get(model,int(raw)) if raw and raw.isdigit() else None
        if row is None:
            row=model(title=title,text=text,enabled=True);session.add(row);await session.flush()
        elif row.title!=title or row.text!=text:
            await repo.set_setting(PREFIX+'previous:'+key+':'+str(getattr(row,'version',1)),json.dumps({'title':row.title,'text':row.text,'enabled':row.enabled},ensure_ascii=False))
            row.title=title
            row.text=text;row.enabled=True
            if isinstance(row,AIKnowledge):row.version+=1;row.updated_at=now()
        await repo.set_setting(PREFIX+key,str(row.id))
        return row.id
    await save(AIKnowledge,'knowledge','Добавление подписки и перенос в другое приложение',KNOWLEDGE,'support_guide_20261108:knowledge:1')
    await save(AIKnowledge,'style','Стиль общения помощника поддержки',STYLE)
    await save(WebQuickReply,'install','Как добавить подписку',INSTALL)
    await save(WebQuickReply,'request','Где найти ссылку подписки',REQUEST)
    await save(WebQuickReply,'transferred','Перенос: подключения отвязаны',TRANSFERRED,'support_guide_20261108:replies:6')
    for old in (3,5):
        raw=await repo.setting('support_guide_20261108:replies:'+str(old))
        row=await session.get(WebQuickReply,int(raw)) if raw and raw.isdigit() else None
        if row:
            previous=PREFIX+'previous:disabled:'+str(row.id)
            if not await repo.setting(previous):
                await repo.set_setting(previous,json.dumps({'enabled':row.enabled}))
            row.enabled=False

    from app.web.support_troubleshooting import seed as seed_troubleshooting
    await seed_troubleshooting(session)


async def capture(session, settings, conversation, message, body):
    repo=SQLRepository(session)
    name=PREFIX+'context:'+str(conversation.id)
    context=json.loads(await repo.setting(name,'{}'))
    if NEGATIVE.search(body):context={}
    elif TRANSFER.search(body):context={'intent':True,'created':now().timestamp()}
    links=URL.findall(body)
    if re.search(r'https?://',body) and not links:
        context['invalid_link']=True
    # Only a literal link from the client is captured, never a model-supplied URL.
    if len(links)==1 and links[0] in body and not context.get('link'):
        context.pop('invalid_link',None)
        context.update(link=encrypt(settings,links[0]),message_id=message.id,created=now().timestamp())
    if len(links)>1:context={}
    await repo.set_setting(name,json.dumps(context))


async def reset_transfer(database, settings, cid, mid):
    """Claim before remote I/O. Ambiguous failures need an operator, never replay."""
    async with database.sessions.begin() as session:
        await lock_nonce(session,PREFIX+'conversation:'+str(cid))
        conv=await session.get(AIConversation,cid,with_for_update=True)
        message=await session.get(AIMessage,mid)
        if not conv or not message or message.conversation_id!=cid or message.generation!=conv.generation or conv.mode!='ai':return 'superseded'
        repo=SQLRepository(session)
        context=json.loads(await repo.setting(PREFIX+'context:'+str(cid),'{}'))
        if context.get('invalid_link'):return 'not_found'
        if not context.get('intent') or not context.get('link') or now().timestamp()-context.get('created',0)>1800:return 'not_requested'
        link=decrypt(settings,context['link'])
        credential=await session.scalar(select(VlessCredential).where(VlessCredential.subscription_url==link,VlessCredential.status=='active'))
        sub=await session.get(Subscription,credential.subscription_id) if credential else None
        if not sub or aware(sub.expires_at)<=now():return 'not_found'
        await lock_nonce(session,PREFIX+'subscription:'+str(sub.id))
        sub=await session.get(Subscription,sub.id,with_for_update=True,populate_existing=True)
        if await repo.setting(f'subscription_status:{sub.id}','active')!='active':return 'not_found'
        if any([await repo.setting(f'subscription_{flag}:{sub.id}')=='1' for flag in ('removed','transferred')]):return 'not_found'
        operation=PREFIX+'operation:'+str(cid)+':'+str(context['message_id'])
        state=await repo.setting(operation)
        if state:return 'done' if state=='done' else 'failed'
        recent=float(await repo.setting(PREFIX+'recent:'+str(sub.id),'0'))
        if now().timestamp()-recent<600:return 'cooldown'
        await repo.set_setting(operation,'processing')
        await repo.set_setting(PREFIX+'recent:'+str(sub.id),str(now().timestamp()))
        credential_id=credential.id
    try:
        async with database.sessions.begin() as session:
            await lock_nonce(session,PREFIX+'conversation:'+str(cid))
            conv=await session.get(AIConversation,cid,with_for_update=True)
            message=await session.get(AIMessage,mid)
            if conv.mode!='ai' or conv.generation!=message.generation:raise ValueError('superseded')
            credential=await session.get(VlessCredential,credential_id)
            sub=await session.get(Subscription,credential.subscription_id,with_for_update=True)
            if credential.status!='active' or aware(sub.expires_at)<=now():raise ValueError('inactive')
            server=await session.get(RemnawaveServer,credential.server_id)
            from app.services.remnawave import RemnawaveService
            from app.services.vless_hwid import confirmed_removal
            repo=SQLRepository(session)
            await RemnawaveService(repo,settings).api(server).delete_all_devices(credential.remote_user_uuid)
            await confirmed_removal(repo,credential,status='removed')
            await repo.set_setting(operation,'done')
            session.add(AdminAudit(actor_telegram_id=0,action='support_install_reset',target=str(credential.id),details=json.dumps({'conversation_id':cid,'message_id':mid})))
        return 'done'
    except Exception:
        async with database.sessions.begin() as session:
            await SQLRepository(session).set_setting(operation,'failed')
        return 'failed'

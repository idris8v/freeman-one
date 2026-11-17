"""Shared agent kernel. Start only with complete channel/backend adapters."""
from dataclasses import dataclass
from pathlib import Path
import asyncio,json,re
from aiohttp import ClientError
from app.services.freeman_ai.provider import ProviderUnavailable
from .intents import financial_risk, messenger_issue, operator_requested, payment_help, subscription_list, technical_issue
from .support_content import (
    CONNECTION_STEPS, SETUP_TOPICS, TELEGRAM_KEYS, connection_text, setup_guide, REGION_APPS, REGION_PENDING, REGION_QUESTION, SCOPE_OPTIONS, SCOPE_QUESTION, scope_followup, quick_diagnostic, happ_install_reply, import_error, region_intent, region_reply,
)
SYSTEM=(Path(__file__).with_name('SYSTEM.md')).read_text()
DEFS={
 'diagnostic_scope':('Показать две кнопки выбора, если снимок уже разобран и неясно, что именно перестало открываться.',{}),
 'get_subscriptions':('Проверить подписки подтверждённого клиента или буквально присланного ключа.',{}),
 'get_devices':('Получить фактический список устройств выбранного ключа.',{'key_ref':{'type':'string'}}),
 'prepare_detach':('Показать выбор устройств/подтверждение. НЕ выполнять отвязку по воле модели.',{'key_ref':{'type':'string'},'device_refs':{'type':'array','items':{'type':'string'}}}),
 'prepare_reissue':('Предупредить об отзыве старой ссылки и показать кнопку подтверждения. НЕ менять ключ.',{'key_ref':{'type':'string'}}),
 'get_payment':('Проверить доступный клиенту платёж без изменения начислений.',{'payment_ref':{'type':'string'}}),
 'get_purchase_link':('Проверенный переход из текущего каталога. Никаких списаний.',{'renewal':{'type':'boolean'},'key_ref':{'type':'string'}}),
 'get_antig_link':('Открыть расчёт антиглушилки для выбранной действующей подписки подтверждённого клиента. Никаких списаний.',{'key_ref':{'type':'string'}}),
 'generate_qr':('QR выбранной или буквально присланной клиентом ссылки, с обратной проверкой.',{'key_ref':{'type':'string'}}),
 'handoff':('Передать оператору по просьбе клиента, после завершённой диагностики или если действие доступно только оператору.',{'reason':{'type':'string'},'summary':{'type':'string'}}),
 'knowledge':('Искать по всей активной базе знаний. Чтобы прочитать статью целиком, передай её ID из списка; можно искать по симптомам или объединять несколько статей.',{'query':{'type':'string'}}),
 'application_links':('Официальные кнопки приложений. Не доказывает доступность магазина клиента.',{'platform':{'type':'string','enum':['android','ios','windows','macos','android-tv','apple-tv']},'application':{'type':'string'}}),
 'respond':('Завершить ход: финальный ответ клиенту и память. Без нового запроса модели.',{'text':{'type':'string','maxLength':3500},'technical_resolved':{'type':'boolean'},**{k:{'type':'string','maxLength':500} for k in ('device_model','os','application','application_version','network','expected_answer','financial_request','new_symptom','check_name','check_scope','check_result')}}),
 'resource_link':('Проверенный ресурс для текущего шага. Чтобы отправить картинку с настройкой, выбери setup_<вариант>. Один вариант на ответ; ориентируйся на приложение и ОС клиента.',{'resource':{'type':'string','enum':['speed','tv', *('setup_'+topic for topic in SETUP_TOPICS)]}}),
 'catalog':('Цены и доступность из действующего каталога. Без выдуманных тарифов.',{}),
}
@dataclass
class Reply:
 text:str
 actions:list
 knowledge_version:str='2.1'

def installation_guide(text, state):
    """Give a checked walkthrough for explicit setup requests."""
    q = text.casefold()
    if re.search(r'не (?:работает|открывается|вижу|могу|получается)|ошибк|отключается|лимит|оплат|перенос|смена|сменить', q):
        return None
    desktop_followup = state.expected_answer == 'Windows или macOS для установки' and bool(
        re.fullmatch(r'\s*(?:у меня |на )?(?:windows(?: 1[01])?|виндовс|mac(?:os|book)?|мак|макбук)[.! ]*', q))
    if not desktop_followup and not re.search(r'как (?:мне )?(?:установить|подключить|настроить|добавить|вставить|поставить)|установка|инструкци|настройка|(?:покаж|пришл|отправ).*?(?:схем|картин|визуал)|куда нажать', q):
        return None
    incy = bool(re.search(r'\bincy\b|инси|инцы', q))
    happ = bool(re.search(r'\bhapp\b|\bх[эе]пп?\b|\bhep\b', q))
    rabbit = bool(re.search(r'rabbit\s*hole|раббит', q))
    if sum((incy,happ,rabbit)) > 1:
        return None
    if re.search(r'телевизор|android\s*tv|андроид\s*тв|google tv', q):
        if re.search(r'apple|tizen|samsung|webos|\blg\b', q) or rabbit:
            return None
        if not re.search(r'android|андроид|google tv', q):
            return None
        topic = 'tv_happ' if happ else 'tv'
    elif re.search(r'\bwindows\b|виндовс|виндоус', q):
        if rabbit:
            return None
        topic = 'windows_incy' if incy else 'windows'
    elif re.search(r'\bmac(?:os|book)?\b|\bмак(?:бук)?\b', q):
        if incy or rabbit:
            return None
        topic = 'macos'
    elif re.search(r'компьютер|ноутбук|\bпк\b', q):
        state.expected_answer = 'Windows или macOS для установки'
        return Reply('💻 На компьютере Windows или macOS? Подскажу шаги для вашей системы.', [])
    elif re.search(r'android|андроид', q):
        topic = 'android_rabbit' if rabbit else 'android_happ' if happ else 'android_incy'
    elif rabbit or incy or happ:
        topic = 'rabbit' if rabbit else 'incy' if incy else 'happ'
    elif re.search(r'iphone|айфон|ios|ipad|айпад', q):
        topic = 'rabbit'
    else:
        return None
    if desktop_followup:
        state.expected_answer = ''
    guide = setup_guide(topic)
    return Reply(guide['text'], guide['actions'])


_GENERIC_HANDOFF = 'Точный симптом не описан в активной версии либо доступные проверки не помогли.'
_SHORT_FOLLOWUP = re.compile(
    r'^\s*(?:(?:windows(?: 1[01])?|macos|виндовс|мак)[.!\s]*$|да\b|нет\b|не помог|без изменений|вс[её] так же|по-прежнему|а если|а дальше|что дальше|почему|не вижу|отключил|переключил|перезагрузил|только\s+(?:звонки|сообщения)|прокси\s+(?:уже\s+)?(?:отключ[её]н|выключен))',
    re.I,
)
_REPORTED_CHECK = re.compile(
    r'(?<!не )\b(?:проверил[аи]?|пробовал[аи]?|отключил[аи]?|переключил[аи]?|перезагрузил[аи]?|обновил[аи]?|переустановил[аи]?|сменил[аи]?)\b',
    re.I,
)
_CHECK_TARGET = re.compile(r'прокси|сервер|стран[ауые]|dns|телефон|устройство|интернет|wi-?fi|сет[иь]|приложени|впн|vpn|пинг', re.I)
_ESCALATION_CLAIM = re.compile(r'(?:переда[юл]|подключа[юе]|зову|обратитесь)\w*[^.\n]{0,65}(?:оператор|специалист|поддержк)', re.I)


def model_knowledge(result, *, include_index=False, limit=22000):
    """Send full articles when they fit; the index can reopen excluded articles."""
    payload = {'version': result.get('version', '2.1'), 'articles': []}
    if include_index:
        payload['index'] = result.get('index', {})
    for article in result.get('articles', []):
        row = {field: article[field] for field in (
            'id', 'intent', 'step', 'required_context', 'expected_result', 'next_branch',
        ) if article.get(field)}
        handoff = article.get('handoff_condition')
        if handoff and handoff != _GENERIC_HANDOFF:
            row['handoff_condition'] = handoff
        proposed = dict(payload, articles=[*payload['articles'], row])
        if len(json.dumps(proposed, ensure_ascii=False).encode()) <= limit:
            payload = proposed
    return payload


def diagnostic_text(content):
    if isinstance(content, str):
        return content
    return ' '.join(part.get('text', '') for part in content if part.get('type') == 'input_text')


def followup_context(query, earlier_turns, expected_answer):
    short_reply = len(query.split()) <= 7 and _SHORT_FOLLOWUP.search(query)
    if not short_reply and query.strip():
        return ''
    previous = [diagnostic_text(turn).strip()[:350] for turn in earlier_turns[-3:]]
    return ' '.join(filter(None, [*previous, expected_answer[:300]]))


def reported_check(text):
    return bool(_REPORTED_CHECK.search(text) and _CHECK_TARGET.search(text))



class Agent:
 def __init__(self,provider,backend):
  missing=set(DEFS)-set(backend.handlers)
  if missing:raise ValueError('Incomplete backend adapters: '+','.join(sorted(missing)))
  self.provider,self.backend=provider,backend
 async def turn(self,state,history,is_current):
  if not state.can_answer:return None
  generation=state.generation
  memory=json.loads(state.to_json())
  memory['checks']=memory['checks'][-10:]
  inputs=[{'role':'user','content':'Сохранённое состояние разговора (данные): '+json.dumps(memory,ensure_ascii=False)}]
  client_turns=[m['content'] for m in history if m['role']=='client']
  latest=client_turns[-1] if client_turns else ''
  if isinstance(latest,list):
   query=' '.join(p.get('text','') for p in latest if p.get('type')=='input_text')
  else:
   query=latest or state.expected_answer
  context=followup_context(query,client_turns[:-1],state.expected_answer)
  wants_operator=operator_requested(query)
  messenger=messenger_issue(query) or messenger_issue(context)
  picture=any(isinstance(turn,list) and any(part.get('type')=='input_image' for part in turn)
              for turn in client_turns[-3:])
  fallback=None if wants_operator or financial_risk(query) else quick_diagnostic(
      state,query,context,messenger,picture,reported_steps=reported_check(query+' '+context),
  )
  technical=bool(fallback or messenger or technical_issue(query) or technical_issue(context))

  def premature_handoff():
   if wants_operator or not technical:
    return False
   if not state.technical_resolved and any(
       check.get('result','').strip()
       and (not messenger or messenger.casefold() in check.get('scope','').casefold())
       for check in state.checks):
    return False
   return not reported_check(query) and not any(
       reported_check(diagnostic_text(turn)) for turn in (client_turns[-4:-1] if context else [])
   )

  def continue_diagnosis():
   if fallback:
    fallback.remember(state)
    return Reply(fallback.text,list(fallback.actions))
   return Reply('🔎 Давайте разберёмся. Что сейчас видно на экране и что уже попробовали?',[])

  if isinstance(latest,str) and latest.strip() in SCOPE_OPTIONS and await is_current(generation):
   return Reply(scope_followup(state,SCOPE_OPTIONS.index(latest.strip()),client_turns[-4:-1]),[])
  if (isinstance(latest,str) and re.search(r'бот|telegram|телеграм|freemanonebot',query.casefold())
      and re.search(r'где.*(?:ключ|подписк)|(?:ключ|подписк).*где|как (?:найти|посмотреть|получить).*(?:ключ|подписк)',query.casefold())
      and not re.search(r'не приш|не вижу|не (?:отображ|появ)|списал|не оплат|не наш[её]л|нет ключ',query.casefold())
      and await is_current(generation)):
   return Reply(TELEGRAM_KEYS,[])
  guide=installation_guide(latest,state) if isinstance(latest,str) else None
  if guide and await is_current(generation):
   return guide
  install=happ_install_reply(query,state) if isinstance(latest,str) else None
  if install and await is_current(generation):
   state.application='Happ'
   state.os='iOS'
   if state.phase!='waiting_operator':
    state.phase='waiting_customer'
   state.expected_answer='Появилась ли подписка FreemanOne в Happ'
   guide=setup_guide('happ')
   return Reply(guide['text'],guide['actions'])
  if isinstance(latest,str) and subscription_list(latest) and await is_current(generation):
   result=await self.backend.handlers['get_subscriptions']({},state)
   if await is_current(generation):
    if result['actions']:
     return Reply('🔑 **Ваши подписки**\n\nВыберите нужную, чтобы посмотреть срок, ключ и подключённые устройства.',result['actions'])
    text=('В этом аккаунте пока нет подписок.' if result['identity_verified']
          else 'Войдите в кабинет или пришлите ссылку подписки, которую вы получили при покупке.')
    return Reply(text,[])
  apple_context=any(isinstance(turn,str) and any(word in turn.casefold() for word in ('app store','iphone','айфон'))
                    for turn in client_turns[-3:-1])
  region=region_intent(query,state,apple_context) if isinstance(latest,str) else None
  if region and await is_current(generation):
   if region=='downloads':
    state.expected_answer='Сообщите, когда приложение установлено'
    return Reply('📱 Откройте нужное приложение в App Store Казахстана. После установки помогу добавить ключ 🙂',[
     dict(type='link',label='Скачать '+name,url=url) for name,url in REGION_APPS
    ])
   state.expected_answer=REGION_PENDING
   if region=='guide':
    body,buttons=region_reply()
    return Reply(body,buttons)
   return Reply(REGION_QUESTION,[await self.backend.region_action()])
  incident=import_error(query) if isinstance(latest,str) and '[клиент прислал ссылку подписки]' not in query else None
  if incident and await is_current(generation):
   return Reply(incident,[])
  if (isinstance(latest,str)
      and payment_help(latest,financial_request=state.financial_request)
      and await is_current(generation)):
   result=await self.backend.handlers['get_purchase_link']({'renewal':False,'key_ref':''},state)
   state.phase='waiting_customer'
   state.expected_answer='Получилось ли оплатить по одной из кнопок'
   return Reply(
    '💳 Попробуйте оформить подписку через наш новый бот или на сайте.\n\n'
    'На сайте можно купить без регистрации 👍\n\n'
    'Если ошибка повторится, пришлите её скриншот — помогу разобраться.',
    result['actions'],
   )
  knowledge=await self.backend.handlers['knowledge']({'query':query,'context':context},state)
  inputs.append({'role':'user','content':'Активные знания 2.1: '+json.dumps(model_knowledge(knowledge,include_index=True,limit=12000),ensure_ascii=False)})
  for m in history[-32:]:
   if m['role']=='assistant' and m.get('knowledge_version')!='2.1':continue
   if m['role'] not in ('client','operator','assistant'):continue
   inputs.append({'role':'assistant' if m['role'] in ('operator','assistant') else 'user','content':m['content']})
  def size(value):
   if isinstance(value,dict):return 4096 if value.get('type')=='input_image' else sum(size(v) for v in value.values())
   if isinstance(value,list):return sum(size(v) for v in value)
   return len(str(value).encode())
  while len(inputs)>3 and size(inputs)>43000:inputs.pop(2)
  instructions = SYSTEM + '\n\n' + TELEGRAM_KEYS
  if re.search(r'установ|добав|импорт|буфер|схем|визуал|как подключ', query, re.I):
   setup_context = '\n\n'.join(connection_text(topic) for topic in CONNECTION_STEPS)
   instructions += '\n\nЕсли клиент просит показать настройку или пошаговую инструкцию, вызови resource_link с подходящим setup_*. Это приложит схему и кнопку. Отличай Android TV от телефона и Happ от INCY. Для iOS по умолчанию используй RabbitHole, если клиент не выбрал другое приложение.\n\nСправочные шаги установки, если подходят устройству и версии:\n' + setup_context
  schemas=[dict(type='function',name=n,description=d,strict=True,parameters=dict(type='object',properties=p,required=list(p),additionalProperties=False)) for n,(d,p) in DEFS.items()]
  actions=[]
  denied_handoff=False
  # Give vision time to inspect an existing attachment. Text-only fallbacks are short.
  model_deadline=asyncio.get_running_loop().time()+max(18 if picture else 8,getattr(self.provider,'quick_reply_timeout',8)) if fallback else None
  for _ in range(7):
   if not await is_current(generation) or not state.can_answer:return None
   try:
    async with asyncio.timeout_at(model_deadline):
     result=await self.provider.complete(inputs,instructions,schemas)
   except (TimeoutError, ProviderUnavailable, ClientError):
    if fallback is None:raise
    if not await is_current(generation) or not state.can_answer:return None
    fallback.remember(state)
    return Reply(fallback.text,list(fallback.actions))
   if not await is_current(generation) or not state.can_answer:return None
   output=result.get('output',[]);calls=[x for x in output if x.get('type')=='function_call']
   if not calls:
    body='\n'.join(p.get('text','') for o in output if o.get('type')=='message' for p in o.get('content',[]) if p.get('type')=='output_text').strip()
    if not body:raise ValueError('empty_model_reply')
    if premature_handoff() and _ESCALATION_CLAIM.search(body):return continue_diagnosis()
    return Reply(body,actions)
   if len(calls)>3:raise ValueError('too_many_tool_calls')
   inputs.extend(output)
   for call in calls:
    name=call.get('name');args=json.loads(call.get('arguments','{}'))
    if name not in DEFS or not isinstance(args,dict) or set(args)!=set(DEFS[name][1]):raise ValueError('invalid_tool_contract')
    for key,definition in DEFS[name][1].items():
     value=args[key];typ=definition['type']
     if typ=='string' and (not isinstance(value,str) or len(value)>definition.get('maxLength',2048)) or typ=='boolean' and type(value) is not bool or typ=='array' and (not isinstance(value,list) or len(value)>100 or any(not isinstance(x,str) or len(x)>128 for x in value)):raise ValueError('invalid_tool_contract')
     if 'enum' in definition and value not in definition['enum']:raise ValueError('invalid_tool_contract')
    if name=='handoff' and premature_handoff():
     if denied_handoff:return continue_diagnosis()
     denied_handoff=True
     inputs.append({'type':'function_call_output','call_id':call['call_id'],
                    'output':json.dumps({
                     'status':'diagnostics_required',
                     'instruction':'Оператор не вызван. Дайте ближайшую уместную проверку из базы знаний. '
                                   'Если клиент уже сделал шаг, сохраните реальный результат и предложите следующий.',
                    },ensure_ascii=False)})
     continue
    if name=='respond' and premature_handoff() and _ESCALATION_CLAIM.search(args['text']):
     return continue_diagnosis()
    # The backend independently binds account/key/action rights and idempotency.
    try:
     data=await self.backend.handlers[name](args,state)
    except (PermissionError,ValueError,TimeoutError) as error:
     data={'error':type(error).__name__,'completed':False,'instruction':'Операция не подтверждена. Не объявляй успех. Объясни ограничение и предложи выполнимый шаг; при недоступном API создай обращение.'}
    except Exception as error:
     from app.services.remnawave import RemnawaveApiError
     if not isinstance(error,RemnawaveApiError) or name not in ('get_devices','prepare_detach','prepare_reissue'):
      raise
     if not await is_current(generation):return None
     await self.backend.handlers['handoff']({
      'reason':'remnawave_api_'+str(error.status),
      'summary':'Панель не подтвердила данные для операции с ключом. Проверьте состояние подписки и устройств.',
     },state)
     return Reply('🔑 Сейчас не удаётся проверить устройства в панели. Передаю вопрос оператору, он увидит переписку. Запрошенное изменение ещё не выполнено.',[])
    if not await is_current(generation):return None
    if not isinstance(data,dict):raise ValueError('invalid_tool_result')
    actions.extend(data.get('actions',[]))
    if name=='diagnostic_scope' and data.get('actions'):return Reply(SCOPE_QUESTION,data['actions'])
    if name=='respond' and data.get('final_text'):return Reply(data['final_text'],actions)
    if name=='handoff' and data.get('status')=='waiting_operator':
     return Reply(
      '**Подключаю оператора**\n\n'
      'Оператор увидит переписку и уже выполненные проверки. '
      'Пока он подключается, я на связи 🙂',
      actions,
     )
    # Server-issued tokens and copy values belong only in channel delivery.
    public_result=(model_knowledge(data) if name=='knowledge'
                   else {k:v for k,v in data.items() if k not in ('actions','operation')})
    if data.get('actions'):
     public_result['buttons']=[a.get('label','') for a in data['actions']]
    inputs.append({'type':'function_call_output','call_id':call['call_id'],
                   'output':json.dumps(public_result,ensure_ascii=False)})
  raise ValueError('tool_budget_exhausted')

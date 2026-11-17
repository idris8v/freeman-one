"""Broadcast keyboards and recipient-bound promotional actions."""
import hashlib
import json,re,secrets
from datetime import timedelta
from html import escape
from urllib.parse import urlparse
from sqlalchemy import select
from app.database.models import Campaign, Delivery, PersonalOffer, User, now
from app.services.screens import Screen
from app.web.posts import load_json,store_json
READY={'buy':('💳 Купить подписку','buy'),'keys':('🔑 Мои ключи','subscriptions'),'balance':('💰 Баланс','balance'),'proxy':('🌐 Telegram Proxy','p:home'),'help':('📖 Помощь','help'),'documents':('📄 Документы','documents'),'referral':('👥 Хочу заработать','referral')}
DISCOUNT_DAYS = 7
DEAL_INPUTS = {
 'discount': 'Название | процент, например: Получить скидку 20% | 20',
 'promo': 'Название | действующий промокод, например: Применить промокод | RETURN20',
}
async def data(repo,id):return await load_json(repo,f'campaign_buttons:{id}') or {'items':[],'columns':1}
async def save(repo,id,value):await store_json(repo,f'campaign_buttons:{id}',value)
def rows(value):
 items=value.get('items',[]);n=value.get('columns',1)
 from app.config.admin_content import without_leading_emoji
 return [[(without_leading_emoji(b['label']) if str(b.get('icon','')).isdigit() else b['label'],b['target']) for b in items[i:i+n]] for i in range(0,len(items),n)]
def options(value):
 result={}
 for b in value.get('items',[]):
  opts={}
  if b.get('style') in ('primary','success','danger'):opts['style']=b['style']
  if str(b.get('icon','')).isdigit():opts['icon_custom_emoji_id']=b['icon']
  if opts:result[b['target']]=opts
 return result
def https(value):
 p=urlparse(value)
 if p.scheme!='https' or not p.hostname or p.username or p.password:raise ValueError('Укажите https-ссылку без логина и пароля.')
 return value


async def active_promo(repo, code):
 from app.services.catalog import promo_valid
 promo = await repo.promo(code)
 if promo and promo.effect == 'percent_discount' and 1 <= promo.value < 100 and await promo_valid(repo, promo):
  return promo


async def activate(repo, action, user, settings):
 match = re.fullmatch(r'campaign_deal:([1-9][0-9]{0,9}):([0-9a-f]{16})', action)
 unavailable = Screen('Это предложение недоступно для вашего аккаунта.', [], structured=True)
 if not match:
  return unavailable
 campaign_id = int(match[1])
 campaign = await repo.session.get(Campaign, campaign_id)
 if not campaign or campaign.status not in ('queued', 'done', 'cancelled'):
  return unavailable
 delivery = await repo.session.scalar(select(Delivery.id).where(
  Delivery.dedup == f'campaign:{campaign_id}:{user.id}', Delivery.campaign_id == campaign_id,
  Delivery.user_id == user.id, Delivery.status.in_(('sending', 'sent'))))
 if not delivery:
  return unavailable
 button = next((b for b in (await data(repo, campaign_id))['items'] if b['target'] == action), None)
 if not button or not button.get('deal'):
  return unavailable
 status = await repo.session.scalar(select(User.status).where(User.id == user.id).with_for_update())
 if not status or status == 'blocked':
  return unavailable

 deal = button['deal']
 if deal['kind'] == 'discount':
  from app.services.offers import offer_status
  request_id = f'campaign-click:{campaign_id}:{user.id}'
  offer = await repo.session.scalar(select(PersonalOffer).where(PersonalOffer.request_id == request_id))
  if offer and offer_status(offer) != 'active':
   messages = {'used': 'Скидка уже использована.', 'expired': 'Срок действия скидки истёк.'}
   return Screen(messages.get(offer_status(offer), 'Скидка сейчас недоступна.'), [], structured=True)
  if offer:
   text = f'🎁 <b>Скидка {offer.value // 100}% уже активна</b>'
  else:
   from app.services.winback import AUDIENCE, eligible
   if campaign.audience == AUDIENCE:
    if not await eligible(repo, settings, user):
     return unavailable
   actor_id = await repo.session.scalar(select(User.id).where(User.telegram_id == campaign.actor_id))
   if not actor_id:
    return unavailable
   await repo.add(PersonalOffer(
    user_id=user.id, campaign_id=campaign_id, created_by=actor_id, request_id=request_id,
    fingerprint=hashlib.sha256(action.encode()).hexdigest(), discount_type='percent',
    value=deal['value'] * 100, target='any', usage_limit=1, no_stack=True,
    expires_at=now() + timedelta(days=DISCOUNT_DAYS), title=button['label'],
    message='Скидка на одну оплату VPN из рассылки.'))
   text = (f'🎁 <b>Скидка {deal["value"]}% активирована</b>\n\n'
           f'Действует {DISCOUNT_DAYS} дней на одну оплату VPN. При выборе тарифа покажем лучшую доступную цену.')
 elif deal['kind'] == 'promo':
  from app.services.domain import PromoService
  promo = await active_promo(repo, deal['code'])
  if not promo:
   return Screen('Этот промокод сейчас недоступен.', [], structured=True)
  applied = await PromoService(repo).apply(user.id, promo.code)
  if not applied and not await repo.activation(user.id, promo.id):
   return Screen('Этот промокод сейчас недоступен.', [], structured=True)
  state = 'активирован' if applied else 'уже активирован'
  text = f'🏷 <b>Промокод {escape(promo.code)} {state}</b>\n\nСкидка {promo.value}% учтётся при выборе тарифа VPN.'
 else:
  return unavailable
 return Screen(text, [[('Выбрать тариф', 'buy')], [('Мои ключи', 'subscriptions')]], structured=True)


async def render(service,action,actor):
 p=action.split(':');page=p[2];id=int(p[3]);item=await service.campaign(id,actor)
 if item.status!='draft':raise ValueError('Кнопки можно менять только в черновике.')
 value=await data(service.repo,id);items=value['items'];back=[('← К рассылке',f'admin:x:campaign:{id}')]
 if page=='campaignbuttons':
  return Screen('🔘 <b>Кнопки рассылки</b>\n\nДобавьте скидку, промокод, готовый раздел, ссылку, вложенный текст или Mini App. До отправки доступны редактирование, цвет, порядок и удаление.\n\nКнопок: '+str(len(items)),[[(f'{i+1}. {b["label"]}',f'admin:x:campaignbutton:{id}:{i}')] for i,b in enumerate(items)]+[[('➕ Добавить кнопку',f'admin:x:campaignbuttonnew:{id}')],[('↔️ По две в строке' if value['columns']==1 else '↕️ По одной в строке',f'admin:x:campaignlayout:{id}')],back],structured=True)
 if page=='campaignlayout':
  value['columns']=2 if value['columns']==1 else 1;await save(service.repo,id,value)
  return await render(service,f'admin:x:campaignbuttons:{id}',actor)
 if page=='campaignbuttonnew':
  if len(items)>=10:raise ValueError('Максимум 10 кнопок в рассылке.')
  return Screen('➕ <b>Новая кнопка</b>\n\nВыберите действие:',[[('🎁 Активировать скидку',f'admin:x:input:campaignbutton:{id}:discount')],[('🏷 Применить промокод',f'admin:x:input:campaignbutton:{id}:promo')],[('📂 Готовый раздел / своя кнопка',f'admin:x:campaignready:{id}')],[('🔗 Ссылка',f'admin:x:input:campaignbutton:{id}:url')],[('💬 Вложенный текст',f'admin:x:input:campaignbutton:{id}:text')],[('📱 Mini App по ссылке',f'admin:x:input:campaignbutton:{id}:webapp')],[('📰 Пост в Mini App',f'admin:x:input:campaignbutton:{id}:post')],[('← Назад',f'admin:x:campaignbuttons:{id}')]],structured=True)
 if page in ('campaignready','campaignreadyadd'):
  choices=dict(READY)
  from app.config.admin_content import CUSTOM_BUTTON_TARGETS
  for key,raw in (await service.repo.settings_prefix('custom_button:')).items():
   try:b=json.loads(raw)
   except (ValueError,TypeError):continue
   if b and b.get('enabled',True) and (b.get('target') in CUSTOM_BUTTON_TARGETS or b.get('target','').startswith('https://')):
    choices['c'+key.split(':')[1]]=(b['label'],'url:'+b['target'] if b['target'].startswith('https://') else b['target'])
  if page=='campaignready':
   return Screen('📂 <b>Готовые кнопки</b>',[[(label,f'admin:x:campaignreadyadd:{id}:{key}')] for key,(label,_) in choices.items()]+[[('← Назад',f'admin:x:campaignbuttonnew:{id}')]],structured=True)
  if p[4] not in choices:raise ValueError('Кнопка недоступна.')
  label,target=choices[p[4]]
  from app.config.admin_content import stable_button_key,without_leading_emoji
  key=stable_button_key(target,label=label)
  label=await service.repo.setting('button:'+key) or label
  style=await service.repo.setting('button_style:'+key) or 'default'
  icon=await service.repo.setting('button_icon:'+key) or ''
  if icon.isdigit():label=without_leading_emoji(label)
  if len(items)>=10:raise ValueError('Максимум 10 кнопок.')
  items.append(dict(label=label[:64],target=target,style=style,icon=icon));await save(service.repo,id,value)
  return await render(service,f'admin:x:campaignbuttons:{id}',actor)
 index=int(p[4])
 if not 0<=index<len(items):raise ValueError('Кнопка уже удалена.')
 b=items[index]
 if page=='campaignbuttondelete':items.pop(index)
 elif page=='campaignbuttonmove':
  other=index+(-1 if p[5]=='up' else 1)
  if 0<=other<len(items):items[index],items[other]=items[other],items[index];index=other
 elif page=='campaignbuttonstyle':
  if p[5] not in ('default','primary','success','danger'):raise ValueError('Цвет недоступен.')
  b['style']=p[5]
 if page!='campaignbutton':
  await save(service.repo,id,value)
  return await render(service,f'admin:x:campaignbuttons:{id}',actor)
 deal=b.get('deal')
 destination=b['target'][:200]
 if deal:
  destination=(f'Скидка {deal["value"]}% на одну оплату VPN, {DISCOUNT_DAYS} дней после активации'
               if deal['kind']=='discount' else f'Промокод {deal["code"]}')
 return Screen('🔘 <b>'+escape(b['label'])+'</b>\n\nДействие: '+escape(destination),[[('✏️ Название',f'admin:x:input:campaignbuttonlabel:{id}:{index}')],[('🌟 Пользовательский эмодзи',f'admin:x:input:campaignbuttonicon:{id}:{index}')],[('🔗 Изменить действие / текст',f'admin:x:input:campaignbuttonedit:{id}:{index}')],[('⬆️ Выше',f'admin:x:campaignbuttonmove:{id}:{index}:up'),('⬇️ Ниже',f'admin:x:campaignbuttonmove:{id}:{index}:down')],[(label,f'admin:x:campaignbuttonstyle:{id}:{index}:{key}') for key,label in [('default','Обычная'),('primary','Синяя'),('success','Зелёная'),('danger','Красная')]],[('🗑 Удалить кнопку',f'admin:x:campaignbuttondelete:{id}:{index}')],[('← Назад',f'admin:x:campaignbuttons:{id}')]],structured=True)
async def input(service,action,actor,text):
 p=action.split(':');kind=p[3];id=int(p[4]);item=await service.campaign(id,actor)
 if item.status!='draft':raise ValueError('Редактирование доступно только в черновике.')
 value=await data(service.repo,id)
 if kind=='campaignbuttonicon':
  if text not in ('—','-') and (not text.isdigit() or len(text)>30):raise ValueError('Отправьте пользовательский эмодзи или его числовой ID.')
  value['items'][int(p[5])]['icon']='' if text in ('—','-') else text
 elif kind=='campaignbuttonlabel':
  if not 1<=len(text)<=64:raise ValueError('Название: 1–64 символа.')
  value['items'][int(p[5])]['label']=text
 else:
  if kind!='campaignbuttonedit' and len(value['items'])>=10:raise ValueError('Максимум 10 кнопок.')
  editing=kind=='campaignbuttonedit'
  index=int(p[5]) if editing else None
  if editing:
   type,separator,text=text.partition('|');type=type.strip()
   if not separator:raise ValueError('Формат: url / webapp / text / post / discount / promo | Название | значение.')
  else:type=p[5]
  label,sep,body=text.partition('|');label=label.strip();body=body.strip()
  if not sep or not 1<=len(label)<=64 or not body:raise ValueError('Формат: '+DEAL_INPUTS.get(type,'Название кнопки | ссылка или текст.'))
  deal=None
  if type in ('url','webapp'):target=('url:' if type=='url' else 'webapp:')+https(body)
  elif type in ('text','post'):
   if len(body)>3500:raise ValueError('Вложенный текст: до 3500 символов.')
   token=secrets.token_urlsafe(18)
   await store_json(service.repo,'campaign_post:'+token,dict(campaign_id=id,title=label,text=body))
   target='campaign_post:'+token if type=='text' else 'webapp:'+service.settings.web_base_url+'/broadcast-post/'+token
  elif type in DEAL_INPUTS:
   if type=='discount':
    number=body.removesuffix('%').strip()
    if not re.fullmatch(r'[1-9][0-9]?',number):raise ValueError('Укажите целый процент от 1 до 99.')
    if any(i!=index and (b.get('deal') or {}).get('kind')=='discount' for i,b in enumerate(value['items'])):
     raise ValueError('В рассылке может быть одна кнопка с процентной скидкой.')
    deal=dict(kind=type,value=int(number))
   else:
    promo=await active_promo(service.repo,body.upper()) if len(body)<=64 else None
    if not promo:raise ValueError('Укажите действующий промокод на скидку.')
    deal=dict(kind=type,code=promo.code)
   target=f'campaign_deal:{id}:{secrets.token_hex(8)}'
  else:raise ValueError('Тип кнопки недоступен.')
  button=dict(label=label,target=target)
  if deal:button.update(deal=deal,style='success')
  if editing:
   previous=value['items'][index]
   button.update(style=previous.get('style',button.get('style','default')),icon=previous.get('icon',''))
   value['items'][index]=button
  else:value['items'].append(button)
 await save(service.repo,id,value)
 return await render(service,f'admin:x:campaignbuttons:{id}',actor)

"""Administrator-only home menu visibility; customer menu is unchanged."""
from html import escape
from app.services.screens import Screen

SECTIONS={
 'cabinet':('Личный кабинет','cabinet',True),
 'buy':('Купить подписку','buy',True),
 'subscriptions':('Мои ключи','subscriptions',True),
 'balance':('Баланс','balance',False),
 'referral':('Хочу заработать','referral',False),
 'proxy':('Telegram Proxy','p:home',True),
 'support':('Обращения','support_staff',True),
 'payouts':('Заявки на выплату','admin:x:withdrawals:0',True),
 'help':('Помощь','help',False),
 'trial':('Пробный период','trial',True),
}
async def visible(repo,key):
 return await repo.setting('admin_home_visible:'+key,'1' if SECTIONS[key][2] else '0')=='1'

async def render(repo,parts):
 offset=max(0,int(parts[3])) if len(parts)>3 and parts[2]=='page' else 0
 key=parts[2] if len(parts)>2 and parts[2]!='page' else None
 if key:
  if key not in SECTIONS:raise ValueError('Раздел недоступен.')
  title,target,_=SECTIONS[key]
  if len(parts)>3:
   if parts[3] not in ('0','1'):raise ValueError('Действие недоступно.')
   await repo.set_setting('admin_home_visible:'+key,parts[3])
  shown=await visible(repo,key)
  return Screen(f'👁 <b>{escape(title)}</b>\n\nНа главном экране администратора: {"показан" if shown else "скрыт"}.\n\nРаздел остаётся доступным здесь. Меню клиентов не меняется.',[[('📂 Открыть раздел',target)],[(('🙈 Скрыть' if shown else '👁 Показать'),f'admin:hidden:{key}:{0 if shown else 1}')],[('← Скрытые','admin:hidden')]],structured=True)
 rows=[]
 for key,(title,_,_) in list(SECTIONS.items())[offset:offset+5]:
  shown=await visible(repo,key)
  rows.append([(f'{"👁" if shown else "🙈"} {title}',f'admin:hidden:{key}')])
 if offset:rows.append([('← Предыдущие',f'admin:hidden:page:{max(0,offset-5)}')])
 if offset+5<len(SECTIONS):rows.append([('Следующие →',f'admin:hidden:page:{offset+5}')])
 return Screen('🙈 <b>Скрытые</b>\n\nРазделы главного экрана администратора.\n\n🙈 Скрыт · 👁 Показан\n\nВыберите раздел, чтобы открыть его или изменить видимость. После изменения заново откройте главный экран.',rows+[[('← Администратор','admin:home')]],structured=True)

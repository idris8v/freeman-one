from app.services.screens import Screen
from app.services.referral_accounts import rules,level
async def referral_screen(repo):
 config=await rules(repo);second,third,*_=config
 enabled=await repo.setting('referral_enabled','1');minimum=await repo.setting('referral_min_purchase','0')
 text=f'👥 <b>Реферальная программа</b>\n\nСтатус: {"Включена" if enabled=="1" else "Выключена"}'
 for index,(count,label) in enumerate(((0,f'1–{second-1}'),(second,f'{second}–{third-1}'),(third,f'от {third}')),1):
  rate=level(count,config)
  text+=f'\n\nСтупень {index} · {label} приглашений\nПервая покупка: {rate["first_percent"]}% · повторные: {rate["repeat_percent"]}%'
 text+=f'\n\nМинимальная покупка: {minimum} ₽.\n\nНастройки едины для сайта и бота. Старые начисления не пересчитываются.'
 return Screen(text,[[('Включить / выключить','admin:confirm:referraltoggle:0')],[('✏️ Ступени и проценты','admin:input:referral_tiers')],[('✏️ Минимальная покупка','admin:input:setting:referral_min_purchase')],[('⚙️ Вывод средств','admin:x:referrals')],[('← Назад','admin:marketing')]],structured=True)

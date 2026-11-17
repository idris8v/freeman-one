"""Public referral help through the existing post miniapp; no account data."""
import hashlib
import json
from app.services.referral_accounts import rules,level

async def guide_link(repo,settings):
    config=await rules(repo)
    second,third,*_=config
    text='Как заработать с Freeman ONE\n\nПоделитесь своей ссылкой. Друг регистрируется по ней и покупает подписку — вы получаете бонус. За повторные покупки тоже начисляется бонус.\n\n'
    for count,title in ((0,f'Первая ступень · до {second-1} приглашённых'),(second,f'Вторая ступень · {second}–{third-1} приглашённых'),(third,f'Третья ступень · от {third} приглашённых')):
        rate=level(count,config)
        first,repeat=rate['first_percent'],rate['repeat_percent']
        text+=f'{title}\nПервая покупка: {first}%. Повторные: {repeat}%.\nПример: с покупки на 1 000 ₽ — {first*10} ₽; с повторной на такую же сумму — {repeat*10} ₽.\n\n'
    minimum=int(await repo.setting('withdraw_min','1000'))*100
    from app.services.screens import money
    text+=('Ступень зависит от числа зарегистрировавшихся по вашей ссылке. '
        'Если у вас персональные условия, действуют проценты, указанные в боте, вместо общих ступеней.\n\n'
        'Бонус начисляется после подтверждения покупки. Пополнение баланса не даёт бонуса. '
        'При возврате покупки соответствующий бонус отменяется.\n\n'
        f'Бонус можно потратить на подписки или вывести. Минимум вывода: {money(minimum)}.\n\n'
        '1 000 ₽ в примерах — условная сумма покупки, а не цена тарифа. Актуальные ваши проценты и начисления смотрите в боте.')
    threshold=await repo.setting('referral_min_purchase','0')
    if threshold!='0':text+=f'\n\nМинимальная покупка для начисления бонуса: {threshold} ₽.'
    value=dict(title='Как заработать с Freeman ONE',text=text)
    raw=json.dumps(value,ensure_ascii=False)
    token='refguide-'+hashlib.sha256(raw.encode()).hexdigest()[:24]
    key='campaign_post:'+token
    if not await repo.setting(key):
        from app.web.posts import store_json
        await store_json(repo,key,value)
    return 'webapp:'+settings.web_base_url.rstrip('/')+'/broadcast-post/'+token


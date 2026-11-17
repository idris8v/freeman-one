"""Account-scoped referral overrides. Shared rates, bot-only editing UI."""
import hashlib
import json
import re
import secrets
from html import escape
from sqlalchemy import select,func
from sqlalchemy.exc import DBAPIError
from aiogram.types import InputRichMessage
from app.database.models import AppSetting,User,Referral,now
from app.services.screens import Screen
from app.services.accounts import display_freeman_id
from app.bot.rich_copy import text_blocks


def key(uid):return f'referral_personal:{uid}'


def decode(raw):
    if not raw:return None
    try:data=json.loads(raw)
    except (ValueError,TypeError):raise ValueError('Персональные реферальные условия повреждены. Обратитесь к администратору.') from None
    if not isinstance(data,dict) or data.get('version')!=1 or any(type(data.get(k)) is not int or not 0<=data[k]<=100 for k in ('first_percent','repeat_percent')):
        raise ValueError('Персональные реферальные условия повреждены. Обратитесь к администратору.')
    return data


async def personal(repo,uid):return decode(await repo.setting(key(uid)))


def rich(text,rows):return Screen(text,rows,rich=InputRichMessage(blocks=text_blocks(text),skip_entity_detection=True),structured=True)


async def customer_details(repo,rates):
    first,repeat=rates['first_percent'],rates['repeat_percent']
    minimum=await repo.setting('referral_min_purchase','0')
    text=(f'💰 <b>Ваши персональные условия</b>\n\n'
          f'<blockquote>🎉 Первая покупка приглашённого: <b>{first}%</b>\n\n'
          f'🔄 Повторные покупки: <b>{repeat}%</b></blockquote>\n\n'
          f'💡 Например, с покупки на 1 000 ₽ вы получите <b>{first*10} ₽</b>, '
          f'а с повторной покупки на такую же сумму — <b>{repeat*10} ₽</b>.\n\n'
          'Эти проценты действуют вместо общих ступеней. Поделитесь своей ссылкой — бонус начисляется за оплаченные покупки ваших приглашённых.\n\n'
          '💳 Бонус можно потратить на подписки или вывести при достижении минимальной суммы.\n\n'
          'ℹ️ Пополнение баланса не даёт бонуса. При возврате покупки соответствующий бонус отменяется.')
    if minimum!='0':text+=f'\n\nМинимальная покупка для бонуса: {escape(minimum)} ₽.'
    if await repo.setting('referral_enabled','1')!='1':text+='\n\n⏸ Реферальная программа сейчас отключена. Начисления приостановлены.'
    return rich(text,[[('← Назад','referral')]])


class PersonalReferrals:
    def __init__(self,repo,settings):self.repo,self.settings,self.session=repo,settings,repo.session

    async def customer(self,uid,actor):
        if not self.settings.is_owner(actor.telegram_id):raise PermissionError('Персональные условия доступны только администратору.')
        from app.services.admin import AdminService
        return await AdminService(self.repo,self.settings).customer(uid)

    def identity(self,u):return escape(u.display_name)+' · '+escape(display_freeman_id(u.freeman_id) or f'FO-{u.id}')

    async def fingerprint(self,uid):
        rows=dict((await self.session.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.in_((key(uid),'referral_tiers'))))).all())
        rows['invited']=await self.session.scalar(select(func.count()).select_from(Referral).where(Referral.inviter_id==uid))
        return hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()

    async def prompt(self,uid,actor):
        u=await self.customer(uid,actor)
        from app.services.referral_accounts import account_level
        rates=await account_level(self.repo,uid)
        return rich('👥 <b>Персональные реферальные условия</b>\n\nКлиент: '+self.identity(u)+f'\n\nСейчас: {rates["first_percent"]}% за первую покупку и {rates["repeat_percent"]}% за повторные.'+'\n\nВведите два процента через |:\n<code>50 | 50</code>\n\nПервое число — за первую покупку приглашённого, второе — за повторные. Допустимо от 0 до 100%. Эти ставки заменяют общие ступени только для выбранного клиента.',[[('← Назад',f'admin:clientref:{uid}')]])

    async def input(self,uid,value,actor):
        match=re.fullmatch(r'\s*(\d{1,3})\s*\|\s*(\d{1,3})\s*',value)
        if not match or any(int(v)>100 for v in match.groups()):raise ValueError('Введите два целых процента от 0 до 100: 50 | 50.')
        first,repeat=map(int,match.groups())
        return await self.preview(uid,actor,dict(version=1,first_percent=first,repeat_percent=repeat))

    async def preview(self,uid,actor,proposal):
        u=await self.customer(uid,actor)
        from app.services.referral_accounts import account_level,level,rules
        old=await account_level(self.repo,uid)
        new=proposal or level(old['invited'],await rules(self.repo))
        token=secrets.token_hex(12)
        data=dict(actor=actor.telegram_id,user=uid,proposal=proposal,version=await self.fingerprint(uid),expires=now().timestamp()+600)
        await self.repo.set_setting('referral_personal_confirm:'+token,json.dumps(data))
        text=('👥 <b>Подтвердите реферальные условия</b>\n\nКлиент: '+self.identity(u)+
              f'\n\nСейчас: <b>{old["first_percent"]}% / {old["repeat_percent"]}%</b>'+
              f'\n\nНовые условия: <b>{new["first_percent"]}% / {new["repeat_percent"]}%</b> — '+('персональные.' if proposal else 'общие ступени.')+
              '\n\nПервый процент — за первую покупку приглашённого, второй — за повторные.\n\n'
              'Уже начисленные бонусы не изменятся. Ранее оплаченные покупки сначала рассчитаются по действующим условиям. Новые ставки применяются к дальнейшим покупкам.\n\n'
              'Пополнения баланса не дают бонуса. Общий выключатель программы и минимальная покупка сохраняются.')
        return rich(text,[[('✅ Сохранить',f'admin:clientref:apply:{token}')],[('← Отмена',f'admin:clientref:{uid}')]])

    async def apply(self,token,actor):
        if not re.fullmatch(r'[0-9a-f]{24}',token):raise ValueError('Подтверждение недоступно.')
        row=await self.session.scalar(select(AppSetting).where(AppSetting.key=='referral_personal_confirm:'+token).with_for_update().execution_options(populate_existing=True))
        data=json.loads(row.value) if row else {}
        if data.get('actor')!=actor.telegram_id:raise PermissionError('Подтверждение другого администратора.')
        uid=data['user'];await self.customer(uid,actor)
        if data.get('done'):return await self.render(f'admin:clientref:{uid}',actor)
        if data.get('expires',0)<now().timestamp():raise ValueError('Подтверждение устарело. Введите условия заново.')
        try:
            async with self.session.begin_nested():
                await self.session.scalar(select(User).where(User.id==uid).with_for_update(nowait=True))
        except DBAPIError as error:
            if getattr(error.orig,'sqlstate',None)!='55P03' and getattr(error.orig,'pgcode',None)!='55P03':raise
            return rich('⏳ Сейчас обрабатывается операция по этому клиенту. Условия пока не изменены. Повторите сохранение через несколько секунд.',[[('🔄 Повторить',f'admin:clientref:apply:{token}')],[('← Назад',f'admin:clientref:{uid}')]])
        if await self.fingerprint(uid)!=data['version']:raise ValueError('Условия уже изменились. Откройте карточку и подтвердите заново.')
        # The existing worker serializes rewards by locking users. Holding the
        # referrer's row also serializes this account's policy edits with it.
        # Settle its paid backlog at the old rates before installing the policy.
        from app.services.referral_accounts import prepare_rewards
        await prepare_rewards(self.repo,inviter_id=uid)
        proposal=data['proposal']
        value=''
        if proposal:
            decode(json.dumps(proposal))
            proposal={**proposal,'updated_at':now().isoformat(),'updated_by':actor.telegram_id}
            value=json.dumps(proposal)
        old=await self.repo.setting(key(uid))
        await self.repo.set_setting(key(uid),value)
        from app.services.admin import AdminService
        await AdminService(self.repo,self.settings).audit(actor,'referral_personal',uid,json.dumps(dict(previous=old,current=proposal)))
        data['done']=True
        await self.repo.set_setting('referral_personal_confirm:'+token,json.dumps(data))
        screen=await self.render(f'admin:clientref:{uid}',actor)
        screen.text='✅ Условия сохранены.\n\n'+screen.text
        # Keep the native Rich payload in sync with the success prefix.
        screen.rich=InputRichMessage(blocks=text_blocks(screen.text),skip_entity_detection=True)
        return screen

    async def render(self,action,actor):
        p=action.split(':')
        if p[2]=='apply':return await self.apply(p[3],actor)
        if p[2]=='reset':return await self.preview(int(p[3]),actor,None)
        uid=int(p[2]);u=await self.customer(uid,actor)
        from app.services.referral_accounts import account_level
        rates=await account_level(self.repo,uid)
        text=('👥 <b>Реферальные условия клиента</b>\n\nКлиент: '+self.identity(u)+'\n\n'+
              ('⭐ <b>Персональные условия</b>' if rates['personal'] else f'📈 <b>Общие условия · ступень {rates["tier"]}</b>')+
              f'\n\n🎉 Первая покупка приглашённого: <b>{rates["first_percent"]}%</b>'+
              f'\n\n🔄 Повторные покупки: <b>{rates["repeat_percent"]}%</b>'+
              '\n\nЭто заработок клиента на покупках его приглашённых. Персональные проценты имеют приоритет над общими ступенями и действуют в боте и на сайте.')
        if await self.repo.setting('referral_enabled','1')!='1':text+='\n\n⏸ Программа сейчас отключена для всех. Новых начислений нет.'
        rows=[[('✏️ Задать проценты',f'admin:input:clientref:{uid}')]]
        if rates['personal']:rows.append([('↩️ Вернуть общие условия',f'admin:clientref:reset:{uid}')])
        rows.append([('← Карточка клиента',f'admin:client:{uid}')])
        return rich(text,rows)

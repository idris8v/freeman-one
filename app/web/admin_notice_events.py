"""Safe website cards for existing bot operational history (full admins only)."""
import json
from datetime import timedelta
from sqlalchemy import select
from app.database.models import Activity, Payment, User, AppSetting, Subscription, Ticket, now
from app.services.accounts import display_freeman_id
from app.web.notification_views import event_time, event_money


async def admin_events(session, actor, settings):
    if not settings.is_owner(actor.telegram_id):return []
    result=[]
    events=await session.scalars(select(Activity).where(Activity.created_at>=now()-timedelta(days=30)).order_by(Activity.id.desc()).limit(100))
    for event in events:
        parts=event.dedup.split(':')
        if len(parts)<2 or not parts[1].isdigit():continue
        kind, identity=parts[0],int(parts[1]);uid=None;title=text=target='';tone='message'
        if kind=='user.registered':
            uid=identity;title='Новый клиент';target='clients';text='Клиент зарегистрировался.'
        elif kind=='payment' and len(parts)==3:
            p=await session.get(Payment,identity)
            # Hide stale state cards and own payments (already in customer feed).
            if not p or p.user_id==actor.id or p.status!=parts[2]:continue
            if p.status not in ('succeeded','order_paid','balance_paid','stars_paid','manual_paid','refunded','failed'):continue
            uid=p.user_id;target='admin-payments';tone='warning' if p.status in ('failed','refunded') else 'payment'
            gift=await session.get(AppSetting,f'customer_gift_order:{p.id}')
            try:data=json.loads(gift.value) if gift else {}
            except (ValueError,TypeError):data={}
            title='Платёж возвращён' if p.status=='refunded' else 'Неуспешный платёж' if p.status=='failed' else 'Клиент оплатил подарок' if isinstance(data,dict) and data.get('buyer')==uid else 'Подтверждена покупка'
            text=f'Платёж #{p.id} · {event_money(p.amount)}.'
        elif kind=='ticket.created':
            ticket=await session.get(Ticket,identity)
            if not ticket:continue
            uid=ticket.user_id;title='Новое обращение';target='support';text=f'Обращение #{identity}.';tone='support'
        else:continue
        user=await session.get(User,uid)
        if not user:continue
        who=user.display_name+' · '+(display_freeman_id(user.freeman_id) or 'Клиент')
        item=dict(event_id=f'admin-{event.id}',title=title,text=who+'\n'+text,target=target,kind=tone,occurred_at=event_time(event.created_at).isoformat())
        if kind=='ticket.created':item['ticket_id']=identity
        result.append(item)
    return result

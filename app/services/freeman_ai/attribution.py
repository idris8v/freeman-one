"""A checkout link records an assisted order, never establishes account ownership."""
import secrets
from datetime import timedelta
from sqlalchemy import select
from app.database.models import AICheckoutLink,AIOrderAttribution,AIConversation,now
from app.services.customer_identity import authenticated
from .security import fingerprint,aware

async def create(s,settings,conv,renewal):
    token=secrets.token_urlsafe(32)
    s.add(AICheckoutLink(conversation_id=conv.id,token_hash=fingerprint(settings,'checkout:'+token),renewal=renewal,expires_at=now()+timedelta(days=7)))
    return settings.web_base_url.rstrip('/')+'/ai/checkout/'+token

async def owned_link(s,settings,request,token):
    if not isinstance(token,str) or len(token)!=43:return None
    link=await s.scalar(select(AICheckoutLink).where(AICheckoutLink.token_hash==fingerprint(settings,'checkout:'+token)))
    if not link or aware(link.expires_at)<now():return None
    conv=await s.get(AIConversation,link.conversation_id)
    if not conv or conv.channel!='website':return None
    user=await authenticated(s,request,settings)
    browser=request.cookies.get('freeman_ai_browser','')
    if conv.user_id:
        if not user or user.id!=conv.user_id:return None
    elif not browser or conv.browser_hash!=fingerprint(settings,'browser:'+browser):return None
    return link

async def bind_order(s,settings,request,order):
    token=request.cookies.get('freeman_ai_checkout','')
    link=await owned_link(s,settings,request,token)
    if not link or not link.clicked_at:return
    if order.payment_id or aware(order.created_at)<aware(link.created_at):return
    conv=await s.get(AIConversation,link.conversation_id)
    if conv.user_id and conv.user_id!=order.user_id:return
    if await s.scalar(select(AIOrderAttribution.id).where(AIOrderAttribution.order_id==order.id)):return
    s.add(AIOrderAttribution(link_id=link.id,order_id=order.id))

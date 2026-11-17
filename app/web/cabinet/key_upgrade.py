"""Website presentation over the same durable upgrade operation as Telegram."""
from datetime import timezone
from fastapi import APIRouter, Cookie, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.database.models import VlessCredential, now
from app.database.repositories import SQLRepository
from app.services.bot_upgrade import BotUpgrade, difference


class UpgradeQuote(BaseModel):
    tariff_id: int = Field(ge=1)


class UpgradeAction(BaseModel):
    token: str = Field(pattern=r"^[0-9a-f]{20}$")
    confirmed: bool = False


def key_upgrade_router(context):
    from app.web.cabinet.keys import owned_key
    router = APIRouter()

    async def owned(session, user, key_id):
        key = await owned_key(session, user.id, key_id)
        credential = await session.scalar(select(VlessCredential).where(
            VlessCredential.access_key_id == key.id, VlessCredential.user_id == user.id))
        if not credential:
            raise HTTPException(409, "Для этой подписки улучшение недоступно. Обратитесь в поддержку.")
        return credential.subscription_id

    def origin(request):
        if request.headers.get("origin") and request.headers["origin"].rstrip("/") != context.settings.web_base_url.rstrip("/"):
            raise HTTPException(403, "Откройте действие на сайте Freeman ONE.")

    async def present(service, user, token, d):
        balance = await service.repo.balance(user.id)
        status = "done" if d.get("done") else "pending" if d.get("held") else "expired" if d["until"] < now().timestamp() else "funds" if balance < d["amount"] else "ready"
        return dict(token=token, status=status, amount=d["amount"], balance=balance,
                    devices=d["devices"], wanted=d["wanted"], expires_at=d["expiry"],
                    quote_expires_at=d["until"], tariff_id=d["target"],
                    old_price=d["old"], new_price=d["new"], months=d["months"],
                    remaining_days=d["seconds"]/86400,
                    auto_renew_note="После улучшения автопродление выключится. Настройте его заново для нового тарифа.")

    @router.get("/api/web/keys/{key_id}/upgrade")
    async def choices(key_id:int, token:str="", freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session)
        async with context.database.sessions.begin() as session:
            service=BotUpgrade(SQLRepository(session),context.settings,source="website")
            sid=await owned(session,user,key_id)
            try:
                if token:
                    data=await service.saved(user,token)
                    if data.get("sid")!=sid:raise HTTPException(404,"Расчёт не найден")
                    return await present(service,user,token,data)
                pending=await service.pending(user,sid)
                if pending:return await present(service,user,*pending)
                sub,_,_=await service.context(user,sid)
                base,plans=await service.plans(user,sub)
                old=await service.rate(user,base);stamp=now()
                seconds=(sub.expires_at.replace(tzinfo=timezone.utc)-stamp).total_seconds()
                values=[]
                for tariff in plans:
                    price=await service.rate(user,tariff)
                    if price>old:values.append(dict(id=tariff.id,devices=tariff.device_limit,
                        amount=difference(old,price,base.months,seconds)))
                return dict(status="choices",devices=sub.device_limit,months=base.months,
                            expires_at=sub.expires_at.replace(tzinfo=timezone.utc).isoformat(),plans=values)
            except ValueError as error:raise HTTPException(409,str(error)) from error

    @router.post("/api/web/keys/{key_id}/upgrade/quote")
    async def quote(key_id:int,payload:UpgradeQuote,request:Request,freeman_session:str|None=Cookie(default=None)):
        origin(request);user=await context.current_user(freeman_session)
        async with context.database.sessions.begin() as session:
            service=BotUpgrade(SQLRepository(session),context.settings,source="website")
            sid=await owned(session,user,key_id)
            try:
                screen=await service.render(f"up:quote:{sid}:{payload.tariff_id}",user)
                # Existing callback IDs are opaque durable intents, not rendered text.
                token=next((action.rsplit(":",1)[1] for row in screen.rows for _,action in row
                            if action.startswith(("up:apply:","up:fund:","up:show:","up:cancel:"))),None)
                if not token:raise HTTPException(409,"Не удалось получить расчёт. Откройте улучшение заново.")
                data=await service.saved(user,token)
                return await present(service,user,token,data)
            except ValueError as error:raise HTTPException(409,str(error)) from error

    @router.post("/api/web/keys/{key_id}/upgrade/{action}")
    async def act(key_id:int,action:str,payload:UpgradeAction,request:Request,freeman_session:str|None=Cookie(default=None)):
        origin(request)
        if action not in ("apply","cancel") or not payload.confirmed:raise HTTPException(422,"Подтвердите действие")
        user=await context.current_user(freeman_session)
        async with context.database.sessions.begin() as session:
            service=BotUpgrade(SQLRepository(session),context.settings,source="website")
            sid=await owned(session,user,key_id)
            try:
                data=await service.saved(user,payload.token)
                if data.get("sid")!=sid:raise HTTPException(404,"Расчёт не найден")
                if action=="cancel":await service.cancel(user,payload.token,data)
                else:await service.apply(user,payload.token,data)
                result=await present(service,user,payload.token,await service.saved(user,payload.token))
                if action=="cancel" and result["status"] not in ("done","pending"):result["status"]="cancelled"
                return result
            except ValueError as error:raise HTTPException(409,str(error)) from error
    return router

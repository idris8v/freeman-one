from sqlalchemy import select
from app.database.models import PromoCode
async def enabled(repo):
 if await repo.setting('promos_enabled','1')!='1':return False
 from app.services.catalog import promo_valid
 for promo in await repo.session.scalars(select(PromoCode).where(PromoCode.effect=='percent_discount')):
  if await promo_valid(repo,promo):return True
 return False

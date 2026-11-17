"""Pricing administration shares Tariff, ProxyPlan, PromoCode and checkout snapshots."""
import json
from datetime import datetime
from app.database.models import now
async def public_price(repo,product,plan_id,price,renewal=False):
 best=price
 for raw in (await repo.settings_prefix('public_promotion:')).values():
  p=json.loads(raw)
  if not p.get('enabled') or p['product'] not in ('all',product) or p['plan_ids'] and plan_id not in p['plan_ids']:continue
  if p['target']=='renewal' and not renewal or p['target']=='purchase' and renewal:continue
  if p.get('starts_at') and datetime.fromisoformat(p['starts_at'])>now() or p.get('ends_at') and datetime.fromisoformat(p['ends_at'])<=now():continue
  candidate=price*(100-p['value'])//100 if p['kind']=='percent' else price-p['value']
  # Public promotions do not stack; use the best available price.
  best=min(best,max(1,candidate))
 from app.services.timed_discount import apply_price
 return min(best,await apply_price(repo,product,price))
async def promo_valid(repo,p):
 if await repo.setting("promos_enabled","1")!="1":return False
 if await repo.setting(f'promo_enabled:{p.code}','1')!='1':return False
 raw=await repo.setting(f'promo_rules:{p.code}')
 if raw:
  x=json.loads(raw)
  if x.get('ends_at') and datetime.fromisoformat(x['ends_at'])<=now():return False
 return True

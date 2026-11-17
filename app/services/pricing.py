from app.services.domain import PromoService


async def base_quote(repo,user_id,tariff):
    special=await repo.tariff_setting(tariff,f'client_price:{user_id}')
    if special:return int(special),0,True
    discount=max(await PromoService(repo).discount(user_id),int(await repo.setting(f'client_discount:{user_id}','0')))
    return tariff.price*(100-discount)//100,discount,False


async def quote(repo,user_id,tariff,sid=0):
    price,discount,special=await base_quote(repo,user_id,tariff)
    from app.services.catalog import public_price
    if not special:price=min(price,await public_price(repo,'vpn',tariff.id,tariff.price,bool(sid)))
    from app.services.offers import selection
    offer,value=await selection(repo,user_id,tariff,sid,price)
    if offer:return value,(tariff.price-value)*100//tariff.price,False
    return price,(tariff.price-price)*100//tariff.price, special

"""One materialized sales snapshot per report request; no cache or writes."""
import json
from decimal import Decimal
from datetime import datetime
from sqlalchemy import text

SUMMARY = 'SELECT count(*) sales,coalesce(sum(amount),0) revenue,count(DISTINCT user_id) buyers,count(*) FILTER(WHERE renewal=true) renewals,coalesce(sum(amount) FILTER(WHERE renewal=true),0) renewal_revenue,count(*) FILTER(WHERE historical) historical FROM filtered WHERE dt>=:a AND dt<:b'
SELECTS = ["SELECT to_char(date_trunc(:bucket,dt AT TIME ZONE 'Europe/Moscow'),'YYYY-MM-DD') date,count(*) sales,sum(amount) revenue,count(*) FILTER(WHERE kind='vpn') vpn,count(*) FILTER(WHERE kind='proxy') proxy,count(*) FILTER(WHERE renewal=true) renewals,count(*) FILTER(WHERE renewal=false) new_sales,coalesce(sum(amount) FILTER(WHERE renewal=true),0) renewal_revenue FROM selected GROUP BY 1 ORDER BY 1", 'SELECT kind,months,devices,tariff_id,\n (array_agg(tariff_name ORDER BY dt DESC,id DESC) FILTER(WHERE tariff_name IS NOT NULL))[1] tariff_name,\n count(*) sales,sum(amount) revenue,count(DISTINCT user_id) buyers,min(amount) min_price,max(amount) max_price\n FROM selected GROUP BY kind,months,devices,tariff_id,CASE WHEN tariff_id IS NULL THEN tariff_name END ORDER BY revenue DESC', 'SELECT sales_source source,count(*) sales,coalesce(sum(amount),0) revenue FROM selected GROUP BY sales_source', 'SELECT channel,sum(amount) revenue FROM selected GROUP BY channel', 'SELECT source,count(DISTINCT user_id) count FROM selected GROUP BY source ORDER BY count DESC', 'SELECT to_char(date_trunc(:bucket,s.dt AT TIME ZONE \'Europe/Moscow\'),\'YYYY-MM-DD\') date,count(DISTINCT s.user_id) FILTER(WHERE f.first_date>=:lo) new_customers,count(DISTINCT s.user_id) FILTER(WHERE f.first_date<:lo) "returning" FROM selected s JOIN (SELECT user_id,min(dt) first_date FROM all_sales GROUP BY user_id) f USING(user_id) GROUP BY 1 ORDER BY 1', "SELECT CASE WHEN kind='proxy' THEN 'Telegram Proxy' WHEN renewal=true THEN 'Продления' WHEN renewal=false THEN 'Новые подписки' ELSE 'Тип покупки не сохранён' END name,sum(amount) revenue,count(*) sales FROM selected GROUP BY 1 ORDER BY revenue DESC", 'SELECT coalesce(sum(external),0) value FROM selected', 'SELECT * FROM selected ORDER BY dt DESC,id DESC LIMIT :limit OFFSET :offset']

async def sales_snapshot(session, cte, params):
    queries = [SUMMARY.replace(':a',':lo').replace(':b',':hi'), SUMMARY.replace(':a',':prior_lo').replace(':b',':lo')] + [sql.replace("OFFSET :offset", "OFFSET least(:offset,greatest(((SELECT count(*) FROM selected)-1)/:limit,0)*:limit)") for sql in SELECTS]
    fields = [f"'q{i}',coalesce((SELECT json_agg(row_to_json(r)) FROM ({sql}) r),'[]'::json)" for i,sql in enumerate(queries)]
    raw = await session.scalar(text(cte + ' SELECT cast(json_build_object(' + ','.join(fields) + ') AS text)'), params)
    value = json.loads(raw,parse_float=Decimal)
    for rows in value.values():
        for row in rows:
            for key in ('dt','created_at'):
                if isinstance(row.get(key),str):row[key]=datetime.fromisoformat(row[key])
    return value

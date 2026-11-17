"""Read-only, globally sorted Proxy customer registry for the web console."""
from datetime import timedelta
from sqlalchemy import select, func, case, or_, and_, cast, String
from app.database.models import ProxyCredential, ProxyServer, ProxyAccessDetails, User, now
from app.services import proxy_control as domain
from app.services.offers import utc

async def clients_page(repo, settings, params):
    args = params.model_dump(exclude={'sort', 'direction'})
    # Keep the existing infrastructure/summary contract, without fetching a client page twice.
    result = await domain.page_data(repo, settings, **(args | {'tab': 'servers'}))
    instant = now()
    state = case((ProxyCredential.status.in_(('disabled', 'blocked')), 'blocked'),
                 (ProxyCredential.expires_at <= instant, 'expired'),
                 (ProxyCredential.expires_at < instant + timedelta(days=7), 'expiring'), else_='active')
    query = select(ProxyCredential, User, ProxyAccessDetails, ProxyServer).join(
        User, User.id == ProxyCredential.user_id).join(ProxyServer, ProxyServer.id == ProxyCredential.server_id).outerjoin(
        ProxyAccessDetails, ProxyAccessDetails.credential_id == ProxyCredential.id).where(
        ProxyCredential.status.not_in(('deleted', 'migrated')))
    if params.user_id:
        query = query.where(User.id == params.user_id)
    if params.q:
        term = '%' + params.q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        numeric = int(params.q) if params.q.isascii() and params.q.isdigit() and len(params.q) <= 19 else None
        numeric = numeric if numeric is not None and numeric <= 9223372036854775807 else None
        query = query.where(or_(User.first_name.ilike(term, escape='\\'), User.username.ilike(term, escape='\\'),
                               User.freeman_id.ilike(term, escape='\\'), User.email.ilike(term, escape='\\'),
                               ProxyCredential.remote_username.ilike(term, escape='\\'),
                               ProxyCredential.id == numeric if numeric is not None else False,
                               User.id == numeric if numeric is not None else False))
    if params.status != 'all':
        query = query.where(state == params.status)
    if params.server_id:
        query = query.where(ProxyCredential.server_id == params.server_id)
    if params.source != 'all':
        query = query.where(ProxyAccessDetails.source == params.source)
    name = func.lower(func.coalesce(func.nullif(User.first_name, ''), func.nullif(User.username, ''), User.freeman_id, cast(User.id, String)))
    traffic = case((ProxyCredential.last_sync.is_not(None), ProxyCredential.total_octets), else_=None)
    grouped = query.with_only_columns(User.id.label('uid'), name.label('name'),
        func.sum(case((and_(state.in_(('active', 'expiring')), ProxyCredential.status != 'missing'), 1), else_=0)).label('active'),
        func.sum(traffic).label('traffic'), func.max(ProxyCredential.expires_at).label('expires')).group_by(User.id, name).subquery()
    total = await repo.session.scalar(select(func.count()).select_from(grouped))
    column = {'name': grouped.c.name, 'id': grouped.c.uid, 'active': grouped.c.active,
              'traffic': grouped.c.traffic, 'expires': grouped.c.expires}[params.sort]
    order = (column.desc() if params.direction == 'desc' else column.asc()).nulls_last()
    ids = list(await repo.session.scalars(select(grouped.c.uid).order_by(order, grouped.c.uid.desc()).offset(
        (params.page - 1) * params.limit).limit(params.limit)))
    clients = {}
    for c, u, meta, server in await repo.session.execute(query.where(User.id.in_(ids)).order_by(ProxyCredential.id.desc())):
        status = 'blocked' if c.status in ('disabled', 'blocked') else 'missing' if c.status == 'missing' else 'expired' if utc(c.expires_at) <= instant else 'expiring' if utc(c.expires_at) < instant + timedelta(days=7) else 'active'
        observed = domain.fresh(server)
        item = dict(id=c.id, user_id=u.id, name=u.display_name or u.username or u.freeman_id,
                    freeman_id=u.freeman_id, username=u.username,
                    avatar=f'/api/web/admin/users/{u.id}/avatar' if u.telegram_id else None,
                    server_id=server.id, server=server.name, source=meta.source if meta else 'unknown',
                    subscription_id=meta.subscription_id if meta else None, payment_id=meta.payment_id if meta else None,
                    ip_limit=meta.ip_limit if meta else None, ips=c.active_unique_ips if observed else None,
                    online=c.current_connections > 0 if observed else None,
                    traffic=c.total_octets if c.last_sync else None, status=status,
                    expires_at=utc(c.expires_at).isoformat(), created_at=utc(c.created_at).isoformat())
        client = clients.setdefault(u.id, dict(id=u.id, name=item['name'], freeman_id=u.freeman_id,
            avatar=item['avatar'], keys=[], active=0, traffic=None, online=None, expires_at=item['expires_at']))
        client['keys'].append(item)
        client['active'] += status in ('active', 'expiring')
        if item['traffic'] is not None:
            client['traffic'] = (client['traffic'] or 0) + item['traffic']
        if item['online'] is not None:
            client['online'] = bool(client['online'] or item['online'])
        client['expires_at'] = max(client['expires_at'], item['expires_at'])
    result.update(items=[clients[uid] for uid in ids if uid in clients], total=total,
                  page=params.page, limit=params.limit, sort=params.sort, direction=params.direction)
    return result

"""Validated, timestamped RemnaWave 3.4.4 panel statistics observations."""

import asyncio
import hashlib
import json
import secrets
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from app.database.models import AppSetting, RemnawaveServer, now
from app.database.repositories import SQLRepository
from app.services import vless_capacity
from app.services.remnawave import RemnawaveApiError
from app.services.vless_creation import durable_session

FIELDS = (
    "users_total",
    "capacity_revision",
    "users_online",
    "traffic_used",
    "health",
    "last_error",
    "last_sync",
)


def counter(value, maximum=2**63 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Missing/invalid statistics counter")
    return value


def response(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise ValueError("Missing/invalid statistics response")
    return payload["response"]


def observation(stats, users):
    """Read documented paths only; never search arbitrary nested numeric fields."""
    data, listing = response(stats), response(users)
    try:
        stamp = counter(data["timestamp"])
        current = int(now().timestamp() * 1000)
        if abs(stamp - current) > 300_000:
            raise ValueError("Statistics timestamp is outside the freshness window")
        counter(data["users"]["totalUsers"], 2**31 - 1)
        online = counter(data["onlineStats"]["onlineNow"], 2**31 - 1)
        total = counter(listing["total"], 2**31 - 1)
        entries = listing["users"]
        if (
            not isinstance(entries, list)
            or len(entries) != min(total, 1)
            or any(
                not isinstance(entry, dict)
                or type(entry.get("id")) is not int
                or entry["id"] <= 0
                for entry in entries
            )
        ):
            raise ValueError("Incomplete user-count response")
        traffic = data["nodes"]["totalBytesLifetime"]
        # The official contract encodes this bigint as a decimal string.
        if (
            not isinstance(traffic, str)
            or not traffic.isascii()
            or not traffic.isdecimal()
            or len(traffic) > 19
        ):
            raise ValueError("Missing/invalid lifetime traffic counter")
        traffic = counter(int(traffic))
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("Missing/invalid statistics fields") from error
    return dict(
        timestamp=stamp, users_total=total, users_online=online, traffic_used=traffic
    )


def source(panel):
    return hashlib.sha256((panel.api_url + "\0" + panel.api_token).encode()).hexdigest()


def key(identity):
    return f"remnawave_stats:{identity}"


async def metadata(session, identity):
    raw = await session.scalar(
        select(AppSetting.value).where(AppSetting.key == key(identity))
    )
    return json.loads(raw) if raw else {}


@asynccontextmanager
async def connection(repo, *, write=False):
    if repo.session.bind.dialect.name == "postgresql":
        async with durable_session(repo) as session:
            if write:
                async with session.begin():
                    yield session
            else:
                yield session
    else:
        # Compatibility for isolated SQLite fixtures; production uses PostgreSQL.
        yield repo.session


async def refresh(service, item, *, request_timeout=None):
    repo = service.repo
    fingerprint = source(item)
    async with connection(repo) as session:
        stored = await session.get(RemnawaveServer, item.id)
        revision = stored.capacity_revision if stored else None
        before = await metadata(session, item.id)

    async def read():
        api = service.api(item)
        await api.health()
        stats = await api.stats()
        users = await api.users(0, 1)
        return observation(stats, users)

    try:
        remote = (
            await asyncio.wait_for(read(), request_timeout)
            if request_timeout is not None
            else await read()
        )
        error = ""
    except (RemnawaveApiError, ValueError, TimeoutError) as failure:
        remote, error = None, str(failure)[:255] or "Statistics API unavailable"
    accepted = False
    async with connection(repo, write=True) as session:
        panel = await session.get(
            RemnawaveServer, item.id, with_for_update=True, populate_existing=True
        )
        if not panel:
            return False  # Uncommitted/new panels have no authoritative cache yet.
        state = await metadata(session, item.id)
        if source(panel) == fingerprint:
            previous = state if state.get("source") == fingerprint else {}
            # Equal provider timestamps cannot order two differing snapshots.
            newer = remote is not None and remote["timestamp"] > previous.get(
                "remote_timestamp", -1
            )
            failed_current = remote is None and state.get("version") == before.get(
                "version"
            )
            if newer or failed_current:
                state = {
                    **previous,
                    "version": secrets.token_hex(16),
                    "source": fingerprint,
                    "last_attempt_at": now().isoformat(),
                    "outcome": "confirmed" if newer else "unavailable",
                }
                if newer:
                    # Rebase across statistics-only revisions, never across an
                    # allocation/recovery revision. Otherwise two fresh polls
                    # could lose the newer user count while protecting traffic.
                    origin = (
                        previous.get("capacity_origin", panel.capacity_revision)
                        if previous.get("capacity_revision") == panel.capacity_revision
                        else panel.capacity_revision
                    )
                    expected = revision
                    if (
                        revision is not None
                        and origin <= revision <= panel.capacity_revision
                    ):
                        expected = panel.capacity_revision
                    if session.bind.dialect.name == "postgresql":
                        # Reuse I03's reservation/revision guard in this same short
                        # transaction; provider HTTP has already completed.
                        await vless_capacity.refresh_total(
                            repo, item, expected, remote["users_total"], session=session
                        )
                    else:
                        panel.users_total = remote["users_total"]
                    panel.users_online, panel.traffic_used = (
                        remote["users_online"],
                        remote["traffic_used"],
                    )
                    panel.health, panel.last_error, panel.last_sync = (
                        "online",
                        "",
                        now(),
                    )
                    state.update(
                        remote_timestamp=remote["timestamp"],
                        last_success_at=panel.last_sync.isoformat(),
                        observed=remote,
                        capacity_deferred=panel.users_total != remote["users_total"],
                        capacity_origin=origin,
                        capacity_revision=panel.capacity_revision,
                    )
                    accepted = True
                else:
                    panel.health, panel.last_error = "offline", error
                await SQLRepository(session).set_setting(
                    key(item.id), json.dumps(state)
                )
            elif remote is not None and state.get("version") == before.get("version"):
                # A current request received an old/equal provider snapshot.
                # Record the attempt while retaining the last successful data.
                state.update(
                    version=secrets.token_hex(16),
                    outcome="stale",
                    last_attempt_at=now().isoformat(),
                )
                await SQLRepository(session).set_setting(
                    key(item.id), json.dumps(state)
                )
        # The caller may commit much later than this observation. Its ORM copy
        # must not overwrite a newer worker's committed counters or health.
        await session.flush()
        values = {field: getattr(panel, field) for field in FIELDS}
    for field, value in values.items():
        set_committed_value(item, field, value)
    return accepted

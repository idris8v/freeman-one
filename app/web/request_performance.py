"""Bounded, process-local web diagnostics; never retain SQL, inputs or identities.

SQL time measures cursor execution (including driver/network wait), not pool wait.
No tracing backend, network I/O, periodic jobs or database writes are introduced.
"""

from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
from time import perf_counter

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import event
from sqlalchemy.exc import TimeoutError as PoolTimeout

_LOG = logging.getLogger("freeman.web.performance")
_CURRENT = ContextVar("web_request_performance", default=None)
_TRACKED = frozenset({
    "GET /api/web/me", "GET /api/web/admin/dashboard",
    "GET /api/web/admin/users", "POST /api/web/admin/users/search",
    "GET /api/web/admin/keys", "POST /api/web/admin/keys/search",
    "GET /api/web/admin/reports", "GET /api/web/admin/payments",
    "GET /api/web/admin/customers/{uid}/profile",
    "GET /api/web/admin/customers/{uid}/section/{name}",
    "GET /api/web/support",
})
_PHASES = frozenset({"auth", "profile_queries", "catalog", "key_rows"})


@dataclass
class _Request:
    started: float = field(default_factory=perf_counter)
    queries: int = 0
    sql_seconds: float = 0.0
    sql_errors: int = 0
    phases: dict = field(default_factory=dict)
    active: bool = True
    previous: float = 0.0


def checkpoint(name):
    state = _CURRENT.get()
    if state is not None and state.active and name in _PHASES:
        stamp = perf_counter()
        state.phases[name] = (stamp - (state.previous or state.started)) * 1000
        state.previous = stamp


class _Probe:
    def __init__(self, engine):
        self.engine = engine
        self.routes = {}
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "after_cursor_execute", self._after)
        event.listen(engine, "handle_error", self._error)

    @staticmethod
    def _before(conn, cursor, statement, parameters, context, executemany):
        state = _CURRENT.get()
        if state is not None and state.active:
            state.queries += 1
            context._freeman_web_timer = (state, perf_counter())

    @staticmethod
    def _finish(context, failed=False):
        timer = getattr(context, "_freeman_web_timer", None)
        if timer is not None:
            context._freeman_web_timer = None
            state, started = timer
            if state.active:
                state.sql_seconds += perf_counter() - started
                state.sql_errors += int(failed)

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        self._finish(context)

    def _error(self, context):
        self._finish(context.execution_context, failed=True)

    def record(self, route, state, status, pool_timeout):
        elapsed = (perf_counter() - state.started) * 1000
        row = self.routes.setdefault(route, dict(
            requests=0, failed_requests=0, queries_total=0, sql_errors=0,
            pool_timeouts=0, total_ms=0.0, sql_ms_total=0.0, max_ms=0.0,
            samples=deque(maxlen=128), last_logged=None,
        ))
        row["requests"] += 1
        row["failed_requests"] += int(status >= 500)
        row["queries_total"] += state.queries
        row["sql_errors"] += state.sql_errors
        row["pool_timeouts"] += int(pool_timeout)
        row["total_ms"] += elapsed
        row["sql_ms_total"] += state.sql_seconds * 1000
        row["max_ms"] = max(row["max_ms"], elapsed)
        row["samples"].append(elapsed)
        stamp = perf_counter()
        if elapsed >= 1500 and (row["last_logged"] is None or stamp - row["last_logged"] >= 60):
            row["last_logged"] = stamp
            _LOG.info("web_slow route=%s status=%d duration_ms=%.1f sql_ms=%.1f queries=%d sql_errors=%d",
                      route, status, elapsed, state.sql_seconds * 1000, state.queries, state.sql_errors)

    def snapshot(self):
        routes = {}
        for name, row in self.routes.items():
            samples = sorted(row["samples"])
            routes[name] = {
                key: row[key] for key in (
                    "requests", "failed_requests", "queries_total", "sql_errors", "pool_timeouts"
                )
            } | dict(
                average_ms=round(row["total_ms"] / row["requests"], 1),
                sql_ms_total=round(row["sql_ms_total"], 1), max_ms=round(row["max_ms"], 1),
                recent_p95_ms=round(samples[max(0, (len(samples) * 95 + 99) // 100 - 1)], 1),
                recent_samples=len(samples),
            )
        pool = self.engine.pool
        return dict(
            scope="current_process_since_start", sample_limit_per_route=128,
            sql_time_scope="cursor_execution_including_driver_wait_not_pool_queue",
            routes=routes,
            pool={name: max(0, method()) for name, method in (
                ("size", getattr(pool, "size", lambda: 0)),
                ("checked_out", getattr(pool, "checkedout", lambda: 0)),
                ("overflow", getattr(pool, "overflow", lambda: 0)),
            )},
        )


def probe_for(engine):
    sync_engine = engine.sync_engine
    probe = getattr(sync_engine, "_freeman_web_performance", None)
    if probe is None:
        probe = _Probe(sync_engine)
        sync_engine._freeman_web_performance = probe
    return probe


def _timing(headers, state):
    values = [f"web;dur={(perf_counter() - state.started) * 1000:.1f}",
              f"sql;dur={state.sql_seconds * 1000:.1f}", f'sql_count;desc="{state.queries}"']
    values.extend(f"{name};dur={value:.1f}" for name, value in state.phases.items())
    # Preserve existing Server-Timing and all unrelated response headers.
    existing = headers.get("Server-Timing", "")
    headers["Server-Timing"] = (existing + ", " if existing else "") + ", ".join(values)


def measured_route(engine, base_class=APIRoute):
    if engine is None:
        return base_class
    probe = probe_for(engine)

    class MeasuredRoute(base_class):
        def get_route_handler(self):
            handler = super().get_route_handler()
            # Template paths only: no customer IDs, query strings or arbitrary labels.
            labels = {method: method + " " + self.path for method in self.methods or ()}
            if not any(label in _TRACKED for label in labels.values()):
                return handler

            async def measured(request):
                label = labels.get(request.method)
                if label not in _TRACKED:
                    return await handler(request)
                state = _Request()
                token = _CURRENT.set(state)
                status, pool_timeout = 500, False
                try:
                    response = await handler(request)
                    status = response.status_code
                    _timing(response.headers, state)
                    return response
                except HTTPException as error:
                    status = error.status_code
                    headers = dict(error.headers or {})
                    _timing(headers, state)
                    error.headers = headers
                    raise
                except PoolTimeout:
                    pool_timeout = True
                    raise
                finally:
                    state.active = False
                    _CURRENT.reset(token)
                    probe.record(label, state, status, pool_timeout)

            return measured

    return MeasuredRoute


def performance_router(context):
    router = APIRouter()
    probe = probe_for(context.database.engine)

    @router.get("/api/web/admin/performance")
    async def performance(freeman_session: str | None = Cookie(default=None)):
        actor = await context.current_user(freeman_session)
        if not context.settings.is_owner(actor.telegram_id):
            raise HTTPException(403, "Доступно только владельцу")
        return JSONResponse(probe.snapshot(), headers={"Cache-Control": "no-store"})

    return router

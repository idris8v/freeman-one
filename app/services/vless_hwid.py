"""Authoritative HWID snapshots and transactionally fenced local device cache."""

import json
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.database.models import AppSetting, Device, Subscription, VlessCredential, now

ACTIVE = ("active", "connected", "online")


def parse_devices(payload, remote_identity):
    """Validate the complete RemnaWave 3.4.4 per-user (unpaginated) contract.

    Reject the entire snapshot if even one entry is incomplete: filtering bad
    entries would turn unknown devices into confirmed absences.
    """
    invalid = ValueError("Панель вернула неполный или некорректный список устройств.")
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise invalid
    data = payload["response"]
    total, devices = data.get("total"), data.get("devices")
    if type(total) is not int or total < 0 or not isinstance(devices, list):
        raise invalid
    if total != len(devices):
        raise invalid
    seen = set()
    for device in devices:
        if not isinstance(device, dict):
            raise invalid
        hwid = device.get("hwid")
        if (
            not isinstance(hwid, str)
            or not hwid.strip()
            or len(hwid) > 255
            or hwid in seen
        ):
            raise invalid
        seen.add(hwid)
        if str(remote_identity).isdigit():
            if type(device.get("userId")) is not int or device["userId"] != int(
                remote_identity
            ):
                raise invalid
        elif device.get("userUuid") != str(remote_identity):
            # Keep UUID-addressed legacy panels isolated from numeric-ID panels.
            raise invalid
        for field in ("platform", "osVersion", "deviceModel", "userAgent", "requestIp"):
            if field not in device or (
                device[field] is not None and not isinstance(device[field], str)
            ):
                raise invalid
        for field in ("createdAt", "updatedAt"):
            value = device.get(field)
            if not isinstance(value, str):
                raise invalid
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise invalid from error
            if parsed.tzinfo is None:
                raise invalid
    return devices


def marker_key(sid):
    return f"vless_hwid_snapshot:{sid}"


async def marker(session, sid):
    # Select the value, not an ORM instance that could be stale in identity_map.
    return await session.scalar(
        select(AppSetting.value).where(AppSetting.key == marker_key(sid))
    )


async def rows(session, sid):
    return list(
        await session.scalars(
            select(Device)
            .where(Device.subscription_id == sid)
            .order_by(Device.id)
            .execution_options(populate_existing=True)
        )
    )


def fingerprint(devices):
    return [
        (d.id, d.provisioning_id, d.status, d.name, d.type, d.created_at)
        for d in devices
    ]


async def advance(repo, sid, *, observed=False):
    previous = await marker(repo.session, sid)
    data = json.loads(previous) if previous else {}
    data["version"] = secrets.token_hex(16)
    if observed:
        data["last_successful_at"] = now().isoformat()
    else:
        data["last_action_at"] = now().isoformat()
    await repo.set_setting(marker_key(sid), json.dumps(data))


async def confirmed_removal(repo, credential, hwid=None, *, status="disconnected"):
    """Explicit successful DELETE/reset, separate from automatic observations.

    Call only after the provider confirms the action. The subscription lock and
    marker invalidate any older in-flight snapshot, including an empty cache.
    """
    from app.services.device_inventory import invalidate
    invalidate(credential.remote_user_uuid)
    sid = credential.subscription_id
    await repo.session.scalar(
        select(Subscription.id).where(Subscription.id == sid).with_for_update()
    )
    for device in await rows(repo.session, sid):
        if hwid is None or device.provisioning_id == hwid:
            device.status = status
    await advance(repo, sid)


@dataclass(frozen=True)
class Observation:
    credential_id: int
    subscription_id: int
    source: tuple
    marker: str | None
    baseline: list


async def capture(repo, credential):
    sid = credential.subscription_id
    return Observation(
        credential.id,
        sid,
        (
            credential.server_id,
            credential.remote_user_uuid,
            credential.status,
            credential.subscription_id,
            credential.access_key_id,
            credential.user_id,
        ),
        await marker(repo.session, sid),
        fingerprint(await rows(repo.session, sid)),
    )


async def apply_snapshot(repo, observation, devices, *, skip_locked=False):
    """Apply a complete validated list only to its unchanged local generation.

    Capture and apply may use different sessions; no database connection needs
    to remain open while the background worker waits for remote HTTP.
    """
    session, sid = repo.session, observation.subscription_id
    locked = await session.scalar(
        select(Subscription.id)
        .where(Subscription.id == sid)
        .with_for_update(skip_locked=skip_locked)
    )
    if locked is None:
        return None
    fresh = await session.scalar(
        select(VlessCredential)
        .where(VlessCredential.id == observation.credential_id)
        .execution_options(populate_existing=True)
    )
    existing = await rows(session, sid)
    if (
        not fresh
        or fresh.status == "deleted"
        or (
            fresh.server_id,
            fresh.remote_user_uuid,
            fresh.status,
            fresh.subscription_id,
            fresh.access_key_id,
            fresh.user_id,
        )
        != observation.source
        or await marker(session, sid) != observation.marker
        or fingerprint(existing) != observation.baseline
    ):
        return None
    by_hwid = {d.provisioning_id: d for d in existing if d.provisioning_id}
    remote_meta = {d["hwid"]: d for d in devices}
    for hwid, item in remote_meta.items():
        device = by_hwid.get(hwid)
        if device is None:
            device = Device(
                subscription_id=sid,
                provisioning_id=hwid,
                created_at=datetime.fromisoformat(
                    item["createdAt"].replace("Z", "+00:00")
                ),
            )
            session.add(device)
        device.name = (item["deviceModel"] or item["platform"] or "Устройство")[:255]
        device.type = (
            "laptop"
            if any(
                v in (item["platform"] or "").lower()
                for v in ("windows", "macos", "linux")
            )
            else "phone"
        )
        device.status = "active"
    for device in existing:
        if device.provisioning_id and device.provisioning_id not in remote_meta:
            device.status = "disconnected"
    await advance(repo, sid, observed=True)
    await session.flush()
    return remote_meta


async def synchronize(repo, credential, api):
    """Existing explicit refresh shares validation/fencing with the worker."""
    observation = await capture(repo, credential)
    devices = parse_devices(
        await api.devices(observation.source[1]), observation.source[1]
    )
    return await apply_snapshot(repo, observation, devices)


async def cache_state(session, sid):
    """Known cached counts, or None for a key never observed/actioned locally."""
    devices = await rows(session, sid)
    raw = await marker(session, sid)
    metadata = json.loads(raw) if raw else {}
    active = {
        d.provisioning_id or f"local:{d.id}" for d in devices if d.status in ACTIVE
    }
    return len(active) if devices or raw else None, metadata.get("last_successful_at")

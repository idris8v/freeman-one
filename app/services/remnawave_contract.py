"""Confirmation rules for RemnaWave 3.4.4 mutations and legacy UUID keys.

A transport success is not an operation acknowledgment. Validate the returned
state before callers update local credentials, quotas, or device caches.
"""

from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from app.services.remnawave import RemnawaveApiError


class ApiResponse(dict):
    """Preserve HTTP status without changing the public mapping interface."""

    def __init__(self, payload, status):
        super().__init__(payload)
        self.status = status


def invalid():
    return RemnawaveApiError("Remnawave не подтвердила результат операции.")


def explicit_result(payload):
    # A legacy result flag must be an actual boolean, never truthy text/numbers.
    # Also reject contradictory error envelopes even when user fields look valid.
    for data in (payload, payload.get("response")):
        if not isinstance(data, dict):
            continue
        for field in (
            "isSuccess",
            "success",
            "isDeleted",
            "isUpdated",
            "isReset",
            "isEnabled",
            "isDisabled",
        ):
            if field in data and data[field] is not True:
                raise invalid()
        if data.get("errorCode") or data.get("error"):
            raise invalid()


def identity(value):
    """Numeric user ID on 3.4.4; UUID only for already stored legacy identities.

    vlessUuid and shortUuid are connection credentials, not user identifiers.
    Never reinterpret them or insert an unchecked identity into an API path.
    """
    if type(value) is int:
        if 0 < value <= 2**53 - 1:
            return "id", value
        raise invalid()
    if not isinstance(value, str) or not value:
        raise invalid()
    if value.isascii() and value.isdigit() and str(int(value)) == value:
        return identity(int(value))
    try:
        if str(UUID(value)) == value.lower():
            return "uuid", value
    except ValueError:
        pass
    raise invalid()


def data(payload):
    if not isinstance(payload, dict):
        raise invalid()
    explicit_result(payload)
    result = payload.get("response")
    if not isinstance(result, dict):
        raise invalid()
    return result


def user(payload, remote_identity):
    result = data(payload)
    field, value = identity(remote_identity)
    actual = result.get(field)
    if type(actual) is not type(value) or actual != value:
        raise invalid()
    return result


def deleted(payload):
    # The installed 3.4.4 controller explicitly documents DELETE 204, no body.
    # A plain empty mapping from a 200 is ambiguous and must not be normalized
    # into that acknowledgment. Older panels use response.isDeleted instead.
    if isinstance(payload, ApiResponse) and payload.status == 204:
        return
    if data(payload).get("isDeleted") is not True:
        raise invalid()


def date(value):
    if not isinstance(value, str):
        raise invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise invalid() from error
    if parsed.tzinfo is None:
        raise invalid()
    return parsed


def update_identity(payload):
    if not isinstance(payload, dict):
        raise invalid()
    fields = [key for key in ("id", "uuid", "username") if key in payload]
    if len(fields) != 1:
        raise invalid()
    field = fields[0]
    value = payload[field]
    if field == "username":
        if not isinstance(value, str) or not value.strip():
            raise invalid()
    else:
        kind, parsed = identity(value)
        if kind != field or type(value) is not type(parsed):
            raise invalid()
    for key in ("hwidDeviceLimit", "trafficLimitBytes"):
        if key in payload and (
            type(payload[key]) is not int or not 0 <= payload[key] <= 2**63 - 1
        ):
            raise invalid()
    if "expireAt" in payload:
        date(payload["expireAt"])
    return field, value


def updated(response, payload):
    field, value = update_identity(payload)
    result = data(response)
    if type(result.get(field)) is not type(value) or result[field] != value:
        raise invalid()
    for key, expected in payload.items():
        if key not in result:
            raise invalid()
        actual = result[key]
        if key == "expireAt":
            # JavaScript Date / RemnaWave serializes milliseconds, whereas
            # Python purchase terms can contain microseconds.
            if abs((date(actual) - date(expected)).total_seconds()) >= 0.001:
                raise invalid()
        elif type(actual) is not type(expected) or actual != expected:
            raise invalid()


def status(response, remote_identity, expected):
    if user(response, remote_identity).get("status") != expected:
        raise invalid()


def traffic_reset(response, remote_identity):
    traffic = user(response, remote_identity).get("userTraffic")
    if (
        not isinstance(traffic, dict)
        or type(traffic.get("usedTrafficBytes")) is not int
        or traffic["usedTrafficBytes"] != 0
    ):
        raise invalid()


def devices_removed(response, remote_identity, hwid=None):
    from app.services.vless_hwid import parse_devices

    data(response)  # Explicit failure overrides even a plausible empty list.
    try:
        devices = parse_devices(response, str(remote_identity))
    except ValueError as error:
        raise invalid() from error
    if (hwid is None and devices) or any(d["hwid"] == hwid for d in devices):
        raise invalid()


def revoked(response, remote_identity):
    result = user(response, remote_identity)
    url, short = result.get("subscriptionUrl"), result.get("shortUuid")
    if not isinstance(url, str) or not isinstance(short, str) or not short:
        raise invalid()
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise invalid() from error
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(c.isspace() for c in url)
    ):
        raise invalid()

"""Customer-facing device limits use recorded evidence, never a full slot alone."""

REGISTERED_DEVICE_STATUSES = frozenset({'active', 'connected', 'online'})
LIMIT_REJECTION_STATUS = 'limit_exceeded'


def device_limit_exceeded(limit, registered_count, rejected_count=0):
    return bool(
        type(limit) is int
        and limit > 0
        and (rejected_count > 0 or (registered_count is not None and registered_count > limit))
    )

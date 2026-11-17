"""One expiry schedule for website, Telegram and email: three days and one day."""
from datetime import timezone


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def stage(expires_at, stamp):
    seconds = (utc(expires_at) - utc(stamp)).total_seconds()
    return 1 if 0 < seconds <= 86400 else 3 if 86400 < seconds <= 3 * 86400 else None


def current(dedup, expires_at, stamp):
    """Do not send an old three-day notice after entering the one-day window."""
    due = stage(expires_at, stamp)
    if due is None:
        return False
    if dedup.endswith(":d1"):
        return due == 1
    if dedup.endswith(":d3"):
        return due == 3
    return True  # A legacy queued notice is separately reconciled by prepare.


def legacy_covers(job, due, expires_at):
    if not job or job.status not in ("pending", "sending", "sent"):
        return False
    return due == 3 or (utc(expires_at) - utc(job.created_at)).total_seconds() <= 86400

"""Generated publication navigation uses trusted configuration, never request hosts."""

from urllib.parse import parse_qs, urlencode, urlsplit

from app.config.settings import Settings

SECTIONS = frozenset(
    {"instructions", "faq", "status", "proxy", "keys", "referrals", "support"}
)


def site_query_url(settings, **parameters):
    base = settings.web_base_url
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in base
        or any(char.isspace() or ord(char) < 32 for char in base)
    ):
        raise ValueError("Publication links require a valid HTTPS WEB_BASE_URL")
    # Accessing port also validates malformed/out-of-range configured ports.
    parsed.port
    return parsed._replace(
        path=parsed.path.rstrip("/") + "/", query=urlencode(parameters), fragment=""
    ).geturl()


def publication_url(settings, publication_id):
    if (
        not str(publication_id).isascii()
        or not str(publication_id).isdigit()
        or int(publication_id) < 1
    ):
        raise ValueError("Invalid publication ID")
    return site_query_url(settings, post=int(publication_id))


def legacy_section_target(settings, target):
    """Rebase only generated legacy section buttons; leave authored URLs intact."""
    if target in SECTIONS:
        return site_query_url(settings, section=target)
    try:
        old = urlsplit(target)
    except ValueError:
        return target
    default = urlsplit(Settings.model_fields["web_base_url"].default)
    query = parse_qs(old.query, keep_blank_values=True)
    if (
        old.scheme == default.scheme
        and old.netloc == default.netloc
        and old.path in ("", "/")
        and not old.fragment
        and set(query) == {"section"}
        and len(query["section"]) == 1
        and query["section"][0] in SECTIONS
    ):
        return site_query_url(settings, section=query["section"][0])
    return target

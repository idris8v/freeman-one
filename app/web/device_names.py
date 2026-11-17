"""Presentation-only exact model lookup. Never modifies device/HWID records."""
from functools import lru_cache
import json
from pathlib import Path

@lru_cache(maxsize=1)
def catalog():
    try:
        return json.loads(Path(__file__).with_name('data').joinpath('device-models.json').read_text())
    except (OSError, ValueError):
        return {}

def device_display_name(name, model=None):
    original = str(name or '').strip()
    names = catalog()
    matched = names.get(original.casefold())
    if matched:
        return matched
    # Keep user names and already-readable provider labels. Do not infer a
    # particular phone from OS, user agent, screen size, IP or HWID.
    if original.casefold() in ('', 'android', 'iphone', 'ipad', 'phone', 'device', 'устройство', 'телефон'):
        return names.get(str(model or '').strip().casefold(), original or 'Устройство')
    return original

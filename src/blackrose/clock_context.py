"""Fresh request-time context from the system clock and its time zone."""
from datetime import UTC, datetime, tzinfo
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCALTIME = '/etc/localtime'


def _zone_key(value: str) -> str:
    """An IANA key from a TZ value or a path into a zoneinfo database."""
    value = value.removeprefix(':')
    return value.split('/zoneinfo/', 1)[1] if '/zoneinfo/' in value else value


def system_time_zone() -> tzinfo:
    """The system's time zone: `TZ`, then `/etc/localtime`, then the bare local offset.

    The IANA zone is preferred so daylight-saving rules apply. When neither source
    names one, the current local offset is used without a name.
    """
    candidates = []
    if os.environ.get('TZ'):
        candidates.append(_zone_key(os.environ['TZ']))
    try:
        target = os.path.realpath(LOCALTIME)
    except OSError:
        target = ''
    if '/zoneinfo/' in target:
        candidates.append(_zone_key(target))
    for key in candidates:
        try:
            return ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return datetime.now().astimezone().tzinfo or UTC


def clock_context(now: datetime | None = None, zone: tzinfo | None = None) -> str:
    instant = datetime.now(UTC) if now is None else now
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('Clock context requires a timezone-aware instant')
    zone = system_time_zone() if zone is None else zone
    utc = instant.astimezone(UTC)
    local = utc.astimezone(zone)
    offset = local.strftime('%z')
    key = getattr(zone, 'key', None)
    return (
        'Current request time (system clock):\n'
        f'Local date/time: {local.isoformat(timespec="seconds")}\n'
        f'UTC offset: {offset[:3]}:{offset[3:]}\n'
        + (f'IANA time zone: {key}\n' if key else '')
        + f'UTC date/time: {utc.isoformat(timespec="seconds").replace("+00:00", "Z")}\n'
        'Use this fresh timestamp for the current time and relative dates; '
        'timestamps in earlier conversation turns may be older.'
    )


def request_messages(messages: list[dict]) -> list[dict]:
    """Attach one fresh clock snapshot without changing retained chat messages."""
    context = clock_context()
    if messages and messages[0].get('role') == 'system':
        system = {**messages[0], 'content': messages[0]['content'] + '\n\n' + context}
        return [system, *messages[1:]]
    return [{'role': 'system', 'content': context}, *messages]

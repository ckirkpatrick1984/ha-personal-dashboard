"""Minimal RFC 5545 reader for Google's "secret address in iCal format".

Why this exists
---------------
The Calendar API expands recurring events server-side (``singleEvents=true``).
An ICS feed does not: it hands you a master ``VEVENT`` carrying an ``RRULE``
and expects the *client* to work out the occurrences. Everything awkward in
this module follows from that one fact.

Deliberate limits, because a dashboard only ever asks about the next few days:

* Occurrences are generated forward from ``DTSTART`` and filtered to the
  requested window, with a hard iteration cap so a malformed rule cannot hang
  the poller.
* ``FREQ`` of DAILY / WEEKLY / MONTHLY / YEARLY is supported, with ``INTERVAL``,
  ``COUNT``, ``UNTIL``, ``BYDAY`` and ``BYMONTHDAY``. Rarer parts (``BYSETPOS``,
  ``BYWEEKNO``, ``BYYEARDAY``) are ignored rather than half-implemented — an
  ignored part yields *too many* candidate occurrences, never too few, so an
  event is at worst shown spuriously rather than silently missed.

Timezone handling follows the spec's three cases:

* ``DTSTART;VALUE=DATE`` — an all-day event. ``DTEND`` is **exclusive**; that
  off-by-one is owned by ``normalise``, so raw date strings are passed through.
* ``DTSTART;TZID=...`` — wall-clock time in a named zone. Occurrences keep the
  wall-clock time across DST boundaries, which is what the spec requires.
* A trailing ``Z``, or no zone at all — UTC, or floating time interpreted in the
  dashboard's configured zone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

__all__ = ["parse_calendar", "expand", "IcsError"]

_MAX_ITERATIONS = 5000
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class IcsError(ValueError):
    """The feed could not be understood at all."""


# --------------------------------------------------------------------------
# Lexing
# --------------------------------------------------------------------------

def _unfold(text):
    """Undo RFC 5545 line folding.

    A continuation line is marked by a leading space or tab, and the separator
    may be CRLF or bare LF depending on who generated the file.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split_params(chunk):
    """Split ``NAME;P=1;Q="a;b"`` honouring quoted parameter values."""
    parts = []
    buf = []
    quoted = False
    for ch in chunk:
        if ch == '"':
            quoted = not quoted
            continue
        if ch == ";" and not quoted:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_line(line):
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    pieces = _split_params(head)
    name = pieces[0].strip().upper()
    params = {}
    for piece in pieces[1:]:
        if "=" in piece:
            key, val = piece.split("=", 1)
            params[key.strip().upper()] = val.strip()
    return name, params, value


def _unescape(value):
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in ("\\", ",", ";"):
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_calendar(text):
    """Return the ``VEVENT`` components as dicts of ``{NAME: [(params, value)]}``."""
    if "BEGIN:VCALENDAR" not in text.upper():
        raise IcsError("not an iCalendar feed")

    events = []
    current = None
    depth = 0
    for line in _unfold(text):
        parsed = _parse_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        upper = value.strip().upper()

        if name == "BEGIN" and upper == "VEVENT":
            current = {}
            depth = 0
            continue
        if current is None:
            continue
        if name == "BEGIN":
            # A nested component, typically VALARM. Skip its properties so a
            # reminder's TRIGGER cannot be mistaken for event data.
            depth += 1
            continue
        if name == "END":
            if depth:
                depth -= 1
                continue
            if upper == "VEVENT":
                events.append(current)
                current = None
            continue
        if depth:
            continue
        current.setdefault(name, []).append((params, value))
    return events


# --------------------------------------------------------------------------
# Value parsing
# --------------------------------------------------------------------------

def _first(component, name):
    entries = component.get(name)
    return entries[0] if entries else None


def _parse_dt(params, value):
    """Return ``(kind, value, tzid)``.

    ``kind`` is ``"date"`` with a ``date`` value, or ``"datetime"`` with a
    **naive** ``datetime`` plus the zone it should be read in (``None`` meaning
    floating).
    """
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and "T" not in value):
        try:
            return "date", date(int(value[0:4]), int(value[4:6]), int(value[6:8])), None
        except (ValueError, IndexError):
            return None, None, None

    tzid = params.get("TZID") or None
    if value.endswith("Z"):
        tzid = "UTC"
        value = value[:-1]
    try:
        moment = datetime(
            int(value[0:4]), int(value[4:6]), int(value[6:8]),
            int(value[9:11]), int(value[11:13]), int(value[13:15] or 0),
        )
    except (ValueError, IndexError):
        return None, None, None
    return "datetime", moment, tzid


def _parse_duration(value):
    """Parse an RFC 5545 duration such as ``P1DT2H30M``."""
    value = value.strip()
    sign = -1 if value.startswith("-") else 1
    value = value.lstrip("+-")
    if not value.startswith("P"):
        return None
    weeks = days = hours = minutes = seconds = 0
    number = ""
    in_time = False
    for ch in value[1:]:
        if ch == "T":
            in_time = True
            continue
        if ch.isdigit():
            number += ch
            continue
        if not number:
            continue
        amount = int(number)
        number = ""
        if ch == "W":
            weeks = amount
        elif ch == "D":
            days = amount
        elif ch == "H":
            hours = amount
        elif ch == "M":
            minutes = amount if in_time else 0
        elif ch == "S":
            seconds = amount
    return sign * timedelta(
        weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds
    )


def _parse_rrule(value):
    rule = {}
    for part in value.strip().split(";"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        rule[key.strip().upper()] = val.strip()
    return rule


# --------------------------------------------------------------------------
# Recurrence
# --------------------------------------------------------------------------

def _add_months(anchor, months):
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, _days_in_month(year, month))
    return anchor.replace(year=year, month=month, day=day)


def _days_in_month(year, month):
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _byday_dates(year, month, tokens):
    """Resolve ``BYDAY`` tokens such as ``2TU`` or ``-1FR`` within one month."""
    out = []
    total = _days_in_month(year, month)
    for token in tokens:
        token = token.strip().upper()
        if not token:
            continue
        ordinal = 0
        weekday_key = token[-2:]
        prefix = token[:-2]
        if prefix:
            try:
                ordinal = int(prefix)
            except ValueError:
                continue
        weekday = _WEEKDAYS.get(weekday_key)
        if weekday is None:
            continue
        matches = [
            day for day in range(1, total + 1)
            if date(year, month, day).weekday() == weekday
        ]
        if not matches:
            continue
        if ordinal == 0:
            out.extend(matches)
        elif ordinal > 0 and ordinal <= len(matches):
            out.append(matches[ordinal - 1])
        elif ordinal < 0 and -ordinal <= len(matches):
            out.append(matches[ordinal])
    return sorted(set(out))


def _occurrences(start, rule, limit_date, min_date=None):
    """Yield occurrence starts, in order, up to ``limit_date``.

    ``start`` is a ``date`` or naive ``datetime``; the same type comes back.

    When the rule has no ``COUNT``, generation fast-forwards to ``min_date``
    rather than stepping from ``DTSTART``. Without that, a daily standup created
    years ago exhausts ``_MAX_ITERATIONS`` before reaching today and silently
    vanishes from the agenda — a dropped event is far worse than a spurious one.
    ``COUNT`` rules must still be walked from the start to stay faithful to the
    spec, but they are inherently bounded by the count itself.
    """
    freq = (rule.get("FREQ") or "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        yield start
        return

    try:
        interval = max(1, int(rule.get("INTERVAL", "1")))
    except ValueError:
        interval = 1

    count = None
    if rule.get("COUNT"):
        try:
            count = int(rule["COUNT"])
        except ValueError:
            count = None

    until = None
    if rule.get("UNTIL"):
        kind, value, _ = _parse_dt({}, rule["UNTIL"])
        if kind == "date":
            until = value
        elif kind == "datetime":
            until = value.date() if isinstance(start, date) and not isinstance(start, datetime) else value

    is_datetime = isinstance(start, datetime)
    base_date = start.date() if is_datetime else start
    skip_to = min_date if (min_date and count is None and min_date > base_date) else None

    def emit(day):
        if is_datetime:
            return datetime.combine(day, start.time())
        return day

    def past_until(candidate):
        if until is None:
            return False
        if isinstance(until, datetime) and isinstance(candidate, datetime):
            return candidate > until
        cand_date = candidate.date() if isinstance(candidate, datetime) else candidate
        until_date = until.date() if isinstance(until, datetime) else until
        return cand_date > until_date

    byday = [t for t in (rule.get("BYDAY") or "").split(",") if t.strip()]
    bymonthday = []
    for token in (rule.get("BYMONTHDAY") or "").split(","):
        token = token.strip()
        if token:
            try:
                bymonthday.append(int(token))
            except ValueError:
                pass

    produced = 0
    steps = 0

    if freq == "DAILY":
        cursor = base_date
        if skip_to:
            cursor += timedelta(days=((skip_to - base_date).days // interval) * interval)
        while steps < _MAX_ITERATIONS:
            steps += 1
            candidate = emit(cursor)
            if past_until(candidate) or cursor > limit_date:
                return
            yield candidate
            produced += 1
            if count is not None and produced >= count:
                return
            cursor += timedelta(days=interval)
        return

    if freq == "WEEKLY":
        weekdays = sorted({_WEEKDAYS[t.strip().upper()[-2:]]
                           for t in byday if t.strip().upper()[-2:] in _WEEKDAYS})
        if not weekdays:
            weekdays = [base_date.weekday()]
        # Anchor on the Monday of DTSTART's week so INTERVAL counts whole weeks.
        week_start = base_date - timedelta(days=base_date.weekday())
        if skip_to:
            whole_weeks = (skip_to - week_start).days // 7
            week_start += timedelta(weeks=(whole_weeks // interval) * interval)
        while steps < _MAX_ITERATIONS:
            steps += 1
            for weekday in weekdays:
                day = week_start + timedelta(days=weekday)
                if day < base_date:
                    continue
                candidate = emit(day)
                if past_until(candidate) or day > limit_date:
                    return
                yield candidate
                produced += 1
                if count is not None and produced >= count:
                    return
            week_start += timedelta(weeks=interval)
        return

    if freq == "MONTHLY":
        cursor = base_date.replace(day=1)
        if skip_to:
            gap = (skip_to.year - cursor.year) * 12 + (skip_to.month - cursor.month)
            if gap > 0:
                cursor = _add_months(cursor, (gap // interval) * interval)
        while steps < _MAX_ITERATIONS:
            steps += 1
            if byday:
                days = _byday_dates(cursor.year, cursor.month, byday)
            elif bymonthday:
                days = []
                total = _days_in_month(cursor.year, cursor.month)
                for value in bymonthday:
                    day = value if value > 0 else total + value + 1
                    if 1 <= day <= total:
                        days.append(day)
                days = sorted(set(days))
            else:
                total = _days_in_month(cursor.year, cursor.month)
                days = [min(base_date.day, total)]
            for day_number in days:
                day = date(cursor.year, cursor.month, day_number)
                if day < base_date:
                    continue
                candidate = emit(day)
                if past_until(candidate) or day > limit_date:
                    return
                yield candidate
                produced += 1
                if count is not None and produced >= count:
                    return
            cursor = _add_months(cursor, interval)
        return

    # YEARLY
    cursor = base_date
    if skip_to and skip_to.year > cursor.year:
        bump = ((skip_to.year - cursor.year) // interval) * interval
        if bump:
            try:
                cursor = cursor.replace(year=cursor.year + bump)
            except ValueError:  # 29 February
                cursor = cursor.replace(year=cursor.year + bump, day=28)
    while steps < _MAX_ITERATIONS:
        steps += 1
        candidate = emit(cursor)
        if past_until(candidate) or cursor > limit_date:
            return
        yield candidate
        produced += 1
        if count is not None and produced >= count:
            return
        try:
            cursor = cursor.replace(year=cursor.year + interval)
        except ValueError:  # 29 February
            cursor = cursor.replace(year=cursor.year + interval, day=28)


# --------------------------------------------------------------------------
# Expansion
# --------------------------------------------------------------------------

def _exdates(component):
    out = set()
    for params, value in component.get("EXDATE", []):
        for chunk in value.split(","):
            kind, parsed, _ = _parse_dt(params, chunk)
            if kind:
                out.add(parsed)
    return out


def expand(components, window_start, window_end, default_tz_name):
    """Turn parsed ``VEVENT``s into flat raw-event dicts for one window.

    ``window_start`` / ``window_end`` are ``date`` bounds, inclusive of the
    start day and exclusive of the end day.
    """
    overrides = set()
    for component in components:
        entry = _first(component, "RECURRENCE-ID")
        uid_entry = _first(component, "UID")
        if entry and uid_entry:
            kind, parsed, _ = _parse_dt(entry[0], entry[1])
            if kind:
                overrides.add((uid_entry[1].strip(), parsed))

    out = []
    for component in components:
        status = _first(component, "STATUS")
        if status and status[1].strip().upper() == "CANCELLED":
            continue

        dtstart = _first(component, "DTSTART")
        if not dtstart:
            continue
        kind, start_value, tzid = _parse_dt(dtstart[0], dtstart[1])
        if not kind:
            continue

        all_day = kind == "date"
        duration = _duration_for(component, kind, start_value)
        uid_entry = _first(component, "UID")
        uid = uid_entry[1].strip() if uid_entry else ""

        rrule_entry = _first(component, "RRULE")
        recurrence_entry = _first(component, "RECURRENCE-ID")
        excluded = _exdates(component)

        if rrule_entry and not recurrence_entry:
            rule = _parse_rrule(rrule_entry[1])
            # Start generating slightly before the window so a long event that
            # began earlier and is still running is not skipped.
            slack = max(duration.days + 1, 1)
            starts = _occurrences(start_value, rule, window_end,
                                  window_start - timedelta(days=slack))
        else:
            starts = [start_value]

        for occurrence in starts:
            if occurrence in excluded:
                continue
            if (uid, occurrence) in overrides and not recurrence_entry:
                continue
            occ_date = occurrence.date() if isinstance(occurrence, datetime) else occurrence
            end_value = occurrence + duration
            end_date = end_value.date() if isinstance(end_value, datetime) else end_value
            # An event overlapping the window counts, not only one starting in it.
            if end_date < window_start or occ_date >= window_end:
                continue
            out.append(_raw_event(component, uid, occurrence, end_value,
                                  all_day, tzid, default_tz_name))
    return out


def _duration_for(component, kind, start_value):
    dtend = _first(component, "DTEND")
    if dtend:
        end_kind, end_value, _ = _parse_dt(dtend[0], dtend[1])
        if end_kind == kind and end_value is not None:
            delta = end_value - start_value
            # Some exporters emit DTEND == DTSTART for all-day events; the
            # exclusive-end rule would then produce a negative-length day.
            if kind == "date" and delta <= timedelta(0):
                return timedelta(days=1)
            return delta
    duration = _first(component, "DURATION")
    if duration:
        parsed = _parse_duration(duration[1])
        if parsed is not None:
            return parsed
    return timedelta(days=1) if kind == "date" else timedelta(hours=1)


def _raw_event(component, uid, start, end, all_day, tzid, default_tz_name):
    summary_entry = _first(component, "SUMMARY")
    location_entry = _first(component, "LOCATION")
    url_entry = _first(component, "URL")
    transp_entry = _first(component, "TRANSP")

    if all_day:
        raw_start = start.isoformat()
        raw_end = end.isoformat()
    else:
        zone = tzid or default_tz_name
        raw_start = {"value": start.isoformat(), "zone": zone}
        raw_end = {"value": end.isoformat(), "zone": zone}

    return {
        "uid": uid,
        "summary": _unescape(summary_entry[1]) if summary_entry else "",
        "all_day": all_day,
        "raw_start": raw_start,
        "raw_end": raw_end,
        "location": _unescape(location_entry[1]) if location_entry else "",
        "url": url_entry[1].strip() if url_entry else "",
        "busy": not (transp_entry and transp_entry[1].strip().upper() == "TRANSPARENT"),
    }

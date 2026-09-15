"""Turn provider output into the flat, pre-computed shape the dashboard renders.

The dashboard is a Go template. Go templates have no usable date arithmetic and
no timezone handling worth relying on, so *everything* is decided here: the
local start time, the display label, the today/tomorrow bucket, the sort order.
The template's only job is to emit HTML.
"""

from __future__ import annotations

from datetime import date, datetime, time as time_of_day, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None


def get_zone(name):
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def window(now_local, days_ahead):
    """The range to request: local midnight today through midnight +N days.

    Keeping the window small matters. It bounds unbounded RRULEs, keeps
    responses paging-free, and is all a day view ever needs.
    """
    start = datetime.combine(now_local.date(), time_of_day.min, tzinfo=now_local.tzinfo)
    end = start + timedelta(days=max(1, days_ahead))
    return start, end


def _parse_all_day(raw):
    """Google sends ``"2026-09-15"``; Graph sends a dict at local midnight."""
    if isinstance(raw, dict):
        value = (raw.get("value") or "")[:10]
    else:
        value = (raw or "")[:10]
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_timed(raw, zone):
    from .providers import _parse_iso  # local import keeps the module standalone-testable

    if isinstance(raw, dict):
        parsed = _parse_iso(raw.get("value"))
        if parsed is None:
            return None
        # Graph's value carries no offset, so the accompanying zone is the only
        # thing that makes it meaningful.
        if raw.get("zone"):
            slot_zone = get_zone(_normalise_windows_zone(raw["zone"]))
            parsed = parsed.replace(tzinfo=slot_zone)
        return parsed.astimezone(zone)
    parsed = _parse_iso(raw)
    return parsed.astimezone(zone) if parsed else None


_WINDOWS_ZONES = {
    "UTC": "UTC",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "India Standard Time": "Asia/Kolkata",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
}


def _normalise_windows_zone(name):
    """Graph may answer with a Windows zone id, which zoneinfo cannot load."""
    return _WINDOWS_ZONES.get(name, name)


def _day_bucket(event_date, today):
    delta = (event_date - today).days
    if delta < 0:
        return "past", "Earlier"
    if delta == 0:
        return "today", "Today"
    if delta == 1:
        return "tomorrow", "Tomorrow"
    return "later", event_date.strftime("%a %-d %b") if _supports_dash() else event_date.strftime("%a %d %b")


_DASH_OK = None


def _supports_dash():
    global _DASH_OK
    if _DASH_OK is None:
        try:
            datetime(2026, 1, 5).strftime("%-d")
            _DASH_OK = True
        except ValueError:
            _DASH_OK = False
    return _DASH_OK


def _time_label(moment):
    label = moment.strftime("%I:%M %p").lstrip("0")
    return label.replace(":00 ", " ")


def normalise_events(raw_events, zone, now_local, days_ahead):
    """Flatten, bucket, sort. Returns a list ready to render."""
    today = now_local.date()
    horizon = today + timedelta(days=max(1, days_ahead))
    out = []

    for raw in raw_events:
        if raw.get("all_day"):
            start_date = _parse_all_day(raw.get("raw_start"))
            end_date = _parse_all_day(raw.get("raw_end"))
            if start_date is None:
                continue
            # DTEND is *exclusive* for all-day events: a one-day event ends on
            # the following day. Rendering it directly makes every all-day event
            # a day too long. Step back one day to get the inclusive last day,
            # and tolerate feeds that already violate the rule the other way.
            if end_date is None or end_date <= start_date:
                last_date = start_date
            else:
                last_date = end_date - timedelta(days=1)

            # An all-day event spanning several days should still show today.
            effective = start_date if start_date >= today else today
            if effective > last_date:
                effective = last_date
            if effective >= horizon or last_date < today:
                continue

            bucket, day_label = _day_bucket(effective, today)
            sort_key = datetime.combine(effective, time_of_day.min, tzinfo=zone)
            out.append({
                "uid": raw.get("uid", ""),
                "source": raw.get("source", ""),
                "source_label": raw.get("source_label", ""),
                "provider": raw.get("provider", ""),
                "summary": raw.get("summary", ""),
                "all_day": True,
                "start": sort_key.isoformat(),
                "start_ts": int(sort_key.timestamp()),
                "end": datetime.combine(last_date, time_of_day.max, tzinfo=zone).isoformat(),
                "time_label": "All day",
                "duration_label": "",
                "day": bucket,
                "day_label": day_label,
                "location": raw.get("location", ""),
                "url": raw.get("url", ""),
                "busy": raw.get("busy", True),
                "in_progress": bucket == "today",
                "minutes_until": None,
            })
            continue

        start = _parse_timed(raw.get("raw_start"), zone)
        end = _parse_timed(raw.get("raw_end"), zone)
        if start is None:
            continue
        if start.date() >= horizon:
            continue
        # Drop events that already finished; a meeting still running stays.
        if end is not None and end <= now_local:
            continue
        if end is None and start < now_local - timedelta(hours=1):
            continue

        bucket, day_label = _day_bucket(start.date(), today)
        minutes_until = int((start - now_local).total_seconds() // 60)
        out.append({
            "uid": raw.get("uid", ""),
            "source": raw.get("source", ""),
            "source_label": raw.get("source_label", ""),
            "provider": raw.get("provider", ""),
            "summary": raw.get("summary", ""),
            "all_day": False,
            "start": start.isoformat(),
            "start_ts": int(start.timestamp()),
            "end": end.isoformat() if end else "",
            "time_label": _time_label(start),
            "duration_label": _duration_label(start, end),
            "day": bucket,
            "day_label": day_label,
            "location": raw.get("location", ""),
            "url": raw.get("url", ""),
            "busy": raw.get("busy", True),
            "in_progress": bool(end and start <= now_local < end),
            "minutes_until": minutes_until,
        })

    # All-day events sort above timed events on the same day.
    out.sort(key=lambda e: (e["start_ts"], 0 if e["all_day"] else 1, e["summary"]))
    return out


def _duration_label(start, end):
    if not end or end <= start:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 60:
        return "%dm" % minutes
    hours, rest = divmod(minutes, 60)
    return "%dh" % hours if rest == 0 else "%dh%02dm" % (hours, rest)


def group_events(events):
    """Bucket events into ordered day groups.

    Done here rather than in the template: grouping in a Go template needs
    variable reassignment across a range, which is fragile and unreadable. The
    template should only ever have to run two nested loops.
    """
    groups = []
    for event in events:
        if groups and groups[-1]["label"] == event["day_label"]:
            groups[-1]["events"].append(event)
        else:
            groups.append({
                "day": event["day"],
                "label": event["day_label"],
                "events": [event],
            })
    for group in groups:
        group["count"] = len(group["events"])
    return groups


def summarise_events(events, now_local):
    today = [e for e in events if e["day"] == "today"]
    upcoming = [e for e in today if not e["all_day"] and e["minutes_until"] is not None and e["minutes_until"] >= 0]
    current = next((e for e in events if e["in_progress"] and not e["all_day"]), None)
    nxt = upcoming[0] if upcoming else None

    return {
        "today_count": len(today),
        "tomorrow_count": len([e for e in events if e["day"] == "tomorrow"]),
        "total_count": len(events),
        "busy_minutes_today": sum(
            _minutes(e) for e in today if not e["all_day"] and e["busy"]
        ),
        "current": _brief(current),
        "next": _brief(nxt),
        "headline": _headline(today, current, nxt, now_local),
    }


def _minutes(event):
    if not event.get("end"):
        return 0
    try:
        start = datetime.fromisoformat(event["start"])
        end = datetime.fromisoformat(event["end"])
    except ValueError:
        return 0
    return max(0, int((end - start).total_seconds() // 60))


def _brief(event):
    if not event:
        return None
    return {
        "summary": event["summary"],
        "time_label": event["time_label"],
        "source_label": event["source_label"],
        "minutes_until": event["minutes_until"],
        "location": event["location"],
    }


def _headline(today, current, nxt, now_local):
    """One sentence answering 'what does my day look like'."""
    if current:
        return "In %s now" % current["summary"]
    if nxt:
        minutes = nxt["minutes_until"] or 0
        if minutes < 60:
            return "%s in %d min" % (nxt["summary"], max(minutes, 0))
        return "%s at %s" % (nxt["summary"], nxt["time_label"])
    if today:
        return "Nothing left today"
    return "Clear day"


def normalise_messages(raw_messages, zone, now_local, limit):
    out = []
    for raw in raw_messages:
        received = raw.get("received")
        if isinstance(received, datetime):
            local = received.astimezone(zone)
        else:
            local = None

        out.append({
            "uid": raw.get("uid", ""),
            "source": raw.get("source", ""),
            "source_label": raw.get("source_label", ""),
            "provider": raw.get("provider", ""),
            "from_name": raw.get("from_name") or raw.get("from_email", ""),
            "from_email": raw.get("from_email", ""),
            "subject": raw.get("subject", ""),
            "snippet": raw.get("snippet", ""),
            "received": local.isoformat() if local else "",
            "received_ts": int(local.timestamp()) if local else 0,
            "time_label": _received_label(local, now_local) if local else "",
            "unread": bool(raw.get("unread", True)),
            "important": bool(raw.get("important", False)),
            "url": raw.get("url", ""),
        })

    out.sort(key=lambda m: m["received_ts"], reverse=True)
    return out[:limit]


def _received_label(moment, now_local):
    delta = now_local - moment
    if delta.total_seconds() < 0:
        return _time_label(moment)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return "%dm ago" % max(minutes, 1)
    if moment.date() == now_local.date():
        return _time_label(moment)
    if moment.date() == now_local.date() - timedelta(days=1):
        return "Yesterday"
    if delta.days < 7:
        return moment.strftime("%a")
    return moment.strftime("%d %b")


def summarise_messages(messages, unread_by_account):
    total = sum(entry["unread"] for entry in unread_by_account)
    important = len([m for m in messages if m["important"] and m["unread"]])
    return {
        "unread_total": total,
        "important_count": important,
        "shown_count": len(messages),
        "by_account": unread_by_account,
        "headline": _mail_headline(total, important),
    }


def _mail_headline(total, important):
    if total == 0:
        return "Inbox zero"
    plural = "s" if total != 1 else ""
    if important:
        return "%d unread, %d flagged important" % (total, important)
    return "%d unread message%s" % (total, plural)

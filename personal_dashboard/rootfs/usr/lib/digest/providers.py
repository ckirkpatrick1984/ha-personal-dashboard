"""Upstream providers: Google (Calendar v3 + Gmail) and Microsoft Graph.

Everything here is stdlib-only on purpose. Both vendors expose plain REST+JSON
and refreshing an OAuth token is a single form POST, so pulling in
``google-api-python-client`` or ``msal`` would add a large dependency tree to an
add-on image for no benefit.

Both providers are asked to expand recurrence **server-side** (Google's
``singleEvents=true``, Graph's ``calendarView``). That deliberately removes the
entire class of RRULE/EXDATE/RECURRENCE-ID bugs that client-side expansion
invites. Do not "optimise" these into ``events`` / ``/me/events`` calls, which
return unexpanded masters.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import email
import email.errors
import email.header
import email.utils
import imaplib
from datetime import datetime, timedelta, timezone

from . import ics

log = logging.getLogger("digest.providers")

USER_AGENT = "personal-dashboard-digest/1.0 (+https://github.com/ckirkpatrick1984/ha-personal-dashboard)"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"

MS_AUTHORITY = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly "
    "https://www.googleapis.com/auth/gmail.readonly"
)
MS_SCOPES = "offline_access User.Read Calendars.Read Mail.Read"

MAX_BACKOFF = 32.0


class ProviderError(RuntimeError):
    """An upstream fetch failed in a way the caller should surface, not crash on."""


def _request(method, url, headers=None, body=None, form=None, timeout=20, attempts=4,
             parse="json"):
    """One HTTP call with truncated exponential backoff and jitter.

    Google's quota documentation *mandates* this backoff shape on 403
    usageLimits, and Graph returns 429 with the same expectation.

    ``parse="text"`` returns the decoded body instead of parsed JSON, for the
    iCalendar feeds which are not JSON at all.
    """
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "text/calendar, */*" if parse == "text" else "application/json")
    headers.setdefault("Accept-Encoding", "identity")

    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if parse == "text":
                    return raw.decode("utf-8", "replace")
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last = ProviderError("HTTP %s from %s: %s" % (exc.code, _host(url), detail))
            # 4xx other than 429 will not become true by being repeated.
            if exc.code not in (429, 500, 502, 503, 504):
                raise last from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = ProviderError("%s talking to %s" % (exc, _host(url)))

        if attempt < attempts - 1:
            delay = min((2 ** attempt) + random.random(), MAX_BACKOFF)
            log.debug("retrying %s in %.1fs (%s)", _host(url), delay, last)
            time.sleep(delay)

    raise last


def _host(url):
    try:
        return urllib.parse.urlsplit(url).netloc or url
    except ValueError:
        return url


def _qs(base, params):
    """Build a query string, repeating keys for list values (Gmail labelIds)."""
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(v)) for v in value)
        elif isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        else:
            pairs.append((key, str(value)))
    return base + "?" + urllib.parse.urlencode(pairs)


def _clean(text, limit=300):
    """Upstream text is untrusted. Strip control characters and cap length.

    Deliberately does *not* HTML-escape: Glance renders these through Go's
    html/template, which escapes interpolated values itself. Escaping here too
    would surface literal ``&lt;`` on the dashboard.
    """
    if not text:
        return ""
    flat = "".join(" " if ch in "\r\n\t" else ch for ch in str(text) if ch >= " " or ch in "\r\n\t")
    flat = " ".join(flat.split())
    return flat[:limit].strip()


# --------------------------------------------------------------------------
# Google
# --------------------------------------------------------------------------

class GoogleAccount:
    """A single Google identity, authorised by a long-lived refresh token.

    The refresh token must come from a consent screen published **In
    production**. While the screen is in "Testing", Google issues refresh
    tokens that expire after 7 days, which is the classic "my dashboard breaks
    every week" failure.
    """

    provider = "google"

    def __init__(self, conf):
        self.id = conf["id"]
        self.label = conf.get("label") or conf["id"]
        self.client_id = conf["client_id"]
        self.client_secret = conf["client_secret"]
        self.refresh_token = conf["refresh_token"]
        self.calendar_ids = conf.get("calendar_ids") or ["primary"]
        self.mail_query_labels = conf.get("mail_labels") or ["INBOX", "UNREAD"]
        # Gmail scopes are "restricted" and Calendar's are only "sensitive", so
        # a calendar-only token is meaningfully easier to obtain. Let an account
        # opt out of mail rather than 403 on every poll.
        self.want_calendar = conf.get("calendar", True)
        self.want_mail = conf.get("mail", True)
        self._token = None
        self._token_expires = 0.0

    def access_token(self):
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        payload = _request(
            "POST",
            GOOGLE_TOKEN_URL,
            form={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if "access_token" not in payload:
            raise ProviderError("Google refused the refresh token for %s" % self.id)
        self._token = payload["access_token"]
        self._token_expires = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def _auth(self):
        return {"Authorization": "Bearer " + self.access_token()}

    def events(self, start, end, tz_name):
        out = []
        for cal_id in self.calendar_ids:
            url = _qs(
                "%s/calendars/%s/events" % (GOOGLE_CALENDAR_BASE, urllib.parse.quote(cal_id, safe="")),
                {
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "singleEvents": True,   # server-side recurrence expansion
                    "orderBy": "startTime",  # only legal alongside singleEvents
                    "maxResults": 100,
                    "timeZone": tz_name,
                    "showDeleted": False,
                },
            )
            payload = _request("GET", url, headers=self._auth())
            for item in payload.get("items", []):
                event = self._normalise_event(item)
                if event:
                    out.append(event)
        return out

    def _normalise_event(self, item):
        if item.get("status") == "cancelled":
            return None
        if _google_declined(item):
            return None

        start = item.get("start") or {}
        end = item.get("end") or {}
        all_day = "date" in start

        return {
            "uid": "google:%s:%s" % (self.id, item.get("id") or item.get("iCalUID") or ""),
            "source": self.id,
            "source_label": self.label,
            "provider": "google",
            "summary": _clean(item.get("summary") or "(no title)", 140),
            "all_day": all_day,
            # For all-day, Google sends an *exclusive* end date. Raw values are
            # handed to the normaliser, which owns that off-by-one.
            "raw_start": start.get("date") or start.get("dateTime"),
            "raw_end": end.get("date") or end.get("dateTime"),
            "location": _clean(item.get("location"), 80),
            "url": item.get("htmlLink") or "",
            "busy": item.get("transparency") != "transparent",
        }

    def unread_count(self):
        payload = _request("GET", "%s/users/me/labels/INBOX" % GMAIL_BASE, headers=self._auth())
        return int(payload.get("messagesUnread") or 0)

    def messages(self, limit):
        listing = _request(
            "GET",
            _qs("%s/users/me/messages" % GMAIL_BASE,
                {"labelIds": self.mail_query_labels, "maxResults": limit}),
            headers=self._auth(),
        )
        out = []
        for stub in listing.get("messages") or []:
            detail = _request(
                "GET",
                _qs("%s/users/me/messages/%s" % (GMAIL_BASE, stub["id"]),
                    {"format": "metadata",
                     "metadataHeaders": ["From", "Subject", "Date"]}),
                headers=self._auth(),
            )
            out.append(self._normalise_message(detail))
        return out

    def _normalise_message(self, detail):
        headers = {}
        for header in (detail.get("payload") or {}).get("headers") or []:
            headers[header.get("name", "").lower()] = header.get("value", "")

        name, email = _split_address(headers.get("from", ""))
        # internalDate is epoch milliseconds, already UTC.
        received = None
        if detail.get("internalDate"):
            received = datetime.fromtimestamp(int(detail["internalDate"]) / 1000, timezone.utc)

        labels = detail.get("labelIds") or []
        return {
            "uid": "google:%s:%s" % (self.id, detail.get("id", "")),
            "source": self.id,
            "source_label": self.label,
            "provider": "google",
            "from_name": _clean(name or email, 60),
            "from_email": _clean(email, 120),
            "subject": _clean(detail.get("subject") or headers.get("subject") or "(no subject)", 160),
            "snippet": _clean(_unescape_snippet(detail.get("snippet")), 200),
            "received": received,
            "unread": "UNREAD" in labels,
            "important": "IMPORTANT" in labels,
            "url": "https://mail.google.com/mail/u/0/#inbox/%s" % detail.get("id", ""),
        }


def _google_declined(item):
    for attendee in item.get("attendees") or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return True
    return False


def _unescape_snippet(text):
    if not text:
        return ""
    for entity, char in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                         ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return text


# --------------------------------------------------------------------------
# Microsoft Graph
# --------------------------------------------------------------------------

class MicrosoftAccount:
    """A single Microsoft identity (work/school or personal), via Graph.

    Registered as a *public client* with "Allow public client flows" enabled, so
    there is no client secret to rotate. Some work tenants require an
    administrator to consent to Calendars.Read / Mail.Read before this will
    return anything but AADSTS65001.
    """

    provider = "microsoft"

    def __init__(self, conf):
        self.id = conf["id"]
        self.label = conf.get("label") or conf["id"]
        self.client_id = conf["client_id"]
        self.tenant = conf.get("tenant") or "common"
        self.client_secret = conf.get("client_secret") or ""
        self.refresh_token = conf["refresh_token"]
        self.want_calendar = conf.get("calendar", True)
        self.want_mail = conf.get("mail", True)
        # Focused Inbox: show only what Outlook classed as "Focused", skipping
        # the "Other" tab. Configurable because Focused Inbox can be switched
        # off tenant-wide or per user, and filtering on it then hides mail.
        self.focused_only = conf.get("focused_only", True)
        self._token = None
        self._token_expires = 0.0

    def token_url(self):
        return "%s/%s/oauth2/v2.0/token" % (MS_AUTHORITY, self.tenant)

    def access_token(self):
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        form = {
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "scope": MS_SCOPES,
        }
        if self.client_secret:
            form["client_secret"] = self.client_secret
        payload = _request("POST", self.token_url(), form=form)
        if "access_token" not in payload:
            raise ProviderError("Microsoft refused the refresh token for %s" % self.id)
        self._token = payload["access_token"]
        self._token_expires = time.time() + float(payload.get("expires_in", 3600))
        # Graph rotates refresh tokens on every use; keep the newest in memory
        # so a long-running process survives the old one being retired.
        if payload.get("refresh_token"):
            self.refresh_token = payload["refresh_token"]
        return self._token

    def _auth(self, tz_name=None):
        headers = {"Authorization": "Bearer " + self.access_token()}
        if tz_name:
            # Without this, Graph returns every time in UTC regardless of the
            # offsets sent on the request bounds.
            headers["Prefer"] = 'outlook.timezone="%s"' % tz_name
        return headers

    def events(self, start, end, tz_name):
        url = _qs(
            "%s/me/calendarView" % GRAPH_BASE,
            {
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$select": "subject,start,end,location,isAllDay,isCancelled,showAs,webLink,responseStatus",
                "$orderby": "start/dateTime",
                "$top": 100,
            },
        )
        payload = _request("GET", url, headers=self._auth(tz_name))
        out = []
        for item in payload.get("value", []):
            event = self._normalise_event(item, tz_name)
            if event:
                out.append(event)
        return out

    def _normalise_event(self, item, tz_name):
        if item.get("isCancelled"):
            return None
        if ((item.get("responseStatus") or {}).get("response")) == "declined":
            return None

        all_day = bool(item.get("isAllDay"))
        start = item.get("start") or {}
        end = item.get("end") or {}

        return {
            "uid": "ms:%s:%s" % (self.id, item.get("id") or ""),
            "source": self.id,
            "source_label": self.label,
            "provider": "microsoft",
            "summary": _clean(item.get("subject") or "(no title)", 140),
            "all_day": all_day,
            "raw_start": _graph_time(start, tz_name),
            "raw_end": _graph_time(end, tz_name),
            "location": _clean(((item.get("location") or {}).get("displayName")), 80),
            "url": item.get("webLink") or "",
            "busy": item.get("showAs") not in ("free", "workingElsewhere"),
        }

    def _mail_filter(self):
        parts = ["isRead eq false"]
        if self.focused_only:
            parts.append("inferenceClassification eq 'focused'")
        return " and ".join(parts)

    def unread_count(self):
        if self.focused_only:
            try:
                return self._focused_unread_count()
            except (ProviderError, KeyError, ValueError, TypeError) as exc:
                # unreadItemCount counts Focused *and* Other, so this fallback
                # can over-report. Better a slightly high badge than none.
                log.warning("focused unread count failed for %s, "
                            "falling back to the whole inbox: %s", self.id, exc)

        payload = _request(
            "GET",
            _qs("%s/me/mailFolders/inbox" % GRAPH_BASE, {"$select": "unreadItemCount"}),
            headers=self._auth(),
        )
        return int(payload.get("unreadItemCount") or 0)

    def _focused_unread_count(self):
        """Count unread Focused mail so the badge matches the listed messages.

        $count on messages is an "advanced query" and Graph rejects it without
        the ConsistencyLevel: eventual header.
        """
        headers = dict(self._auth())
        headers["ConsistencyLevel"] = "eventual"
        payload = _request(
            "GET",
            _qs("%s/me/mailFolders/inbox/messages" % GRAPH_BASE,
                {"$filter": self._mail_filter(), "$count": "true",
                 "$top": 1, "$select": "id"}),
            headers=headers,
        )
        count = payload.get("@odata.count")
        if count is None:
            raise ProviderError("Graph returned no @odata.count for the focused filter")
        return int(count)

    def messages(self, limit):
        # $filter and $orderby together on messages is rejected by Graph
        # ("Sorting not supported for these restrictions"), so sort locally.
        url = _qs(
            "%s/me/mailFolders/inbox/messages" % GRAPH_BASE,
            {
                "$filter": self._mail_filter(),
                "$select": "subject,from,receivedDateTime,bodyPreview,webLink,"
                           "importance,isRead,inferenceClassification",
                "$top": limit,
            },
        )
        payload = _request("GET", url, headers=self._auth())
        return [self._normalise_message(item) for item in payload.get("value", [])]

    def _normalise_message(self, item):
        address = ((item.get("from") or {}).get("emailAddress")) or {}
        received = _parse_iso(item.get("receivedDateTime"))
        return {
            "uid": "ms:%s:%s" % (self.id, item.get("id", "")),
            "source": self.id,
            "source_label": self.label,
            "provider": "microsoft",
            "from_name": _clean(address.get("name") or address.get("address"), 60),
            "from_email": _clean(address.get("address"), 120),
            "subject": _clean(item.get("subject") or "(no subject)", 160),
            "snippet": _clean(item.get("bodyPreview"), 200),
            "received": received,
            "unread": not item.get("isRead", False),
            "important": item.get("importance") == "high",
            "url": item.get("webLink") or "",
        }


def _graph_time(slot, tz_name):
    """Graph returns ``{"dateTime": "...", "timeZone": "..."}`` with no offset.

    With a ``Prefer: outlook.timezone`` header the value is in that zone; with
    no header it is UTC. Either way the string itself carries no offset, so the
    zone has to be re-attached here or every event lands in the wrong hour.
    """
    if not slot:
        return None
    value = slot.get("dateTime")
    if not value:
        return None
    zone = slot.get("timeZone") or tz_name or "UTC"
    return {"value": value, "zone": zone}


def _parse_iso(text):
    if not text:
        return None
    cleaned = text.replace("Z", "+00:00")
    # Graph emits 7 fractional digits; fromisoformat accepts at most 6.
    if "." in cleaned:
        head, _, tail = cleaned.partition(".")
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                tail = tail[len(digits):]
                break
        else:
            tail = ""
        cleaned = head + "." + digits[:6] + tail
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _split_address(value):
    """Split an RFC 5322 From header into (display name, address)."""
    if not value:
        return "", ""
    value = value.strip()
    if "<" in value and ">" in value:
        name = value[:value.index("<")].strip().strip('"').strip()
        email = value[value.index("<") + 1:value.rindex(">")].strip()
        return name, email
    return "", value


# --------------------------------------------------------------------------
# IMAP
#
# Worth having even though OAuth providers exist above. Gmail's API scope
# (gmail.readonly) is classed *restricted* by Google, which is the highest
# verification bar; Calendar's is merely *sensitive*. An IMAP app password
# sidesteps that entirely, so a personal Google account can be read with a
# calendar-only OAuth token plus IMAP for mail.
#
# This is NOT an option for Microsoft 365 work accounts: basic authentication
# for Exchange Online IMAP is disabled, and app passwords are an extension of
# basic auth, so they do not help. Work mail must go through Graph.


class ImapAccount:
    """Read unread mail over IMAP using an app password.

    Deliberately read-only: the mailbox is SELECTed with readonly=True and
    headers are fetched with BODY.PEEK, so polling the dashboard never marks
    your mail as read. Using plain BODY[] here would silently set \\Seen on
    everything the dashboard displayed, which is a genuinely destructive bug.
    """

    provider = "imap"

    def __init__(self, conf):
        self.id = conf["id"]
        self.label = conf.get("label") or conf["id"]
        self.host = conf.get("host") or "imap.gmail.com"
        self.port = int(conf.get("port") or 993)
        self.username = conf["username"]
        self.password = conf["password"]
        self.mailbox = conf.get("mailbox") or "INBOX"
        self.timeout = int(conf.get("timeout") or 20)
        # Gmail throttles accounts that poll IMAP too often, and the resulting
        # lockout lasts hours. Guidance is roughly 10 minutes between checks,
        # so mail is polled on its own slower cadence than the calendar.
        self.min_poll_seconds = int(conf.get("min_poll_seconds") or 600)
        self.web_url = conf.get("web_url") or ""
        # IMAP is a mail protocol; it has no calendar at all.
        self.want_calendar = False
        self.want_mail = conf.get("mail", True)

    def events(self, start, end, tz_name):
        return []

    def _connect(self):
        try:
            client = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ProviderError("IMAP connect to %s:%s failed: %s"
                                % (self.host, self.port, exc)) from exc
        try:
            client.login(self.username, self.password)
        except imaplib.IMAP4.error as exc:
            try:
                client.logout()
            except Exception:
                pass
            # Gmail returns a generic AUTHENTICATIONFAILED here when the user
            # supplies their normal password instead of an app password.
            raise ProviderError(
                "IMAP login failed for %s. If this is Gmail, you need a 16-character "
                "app password (with 2-Step Verification enabled), not your account "
                "password: %s" % (self.username, exc)) from exc
        return client

    def _search_unseen(self, client):
        try:
            client.select(self.mailbox, readonly=True)
            status, data = client.search(None, "UNSEEN")
        except imaplib.IMAP4.error as exc:
            raise ProviderError("IMAP search failed in %s: %s" % (self.mailbox, exc)) from exc
        if status != "OK":
            raise ProviderError("IMAP search returned %s" % status)
        return (data[0] or b"").split()

    def unread_count(self):
        client = self._connect()
        try:
            return len(self._search_unseen(client))
        finally:
            _imap_close(client)

    def messages(self, limit):
        client = self._connect()
        try:
            ids = self._search_unseen(client)
            # Highest UIDs are the newest, and we only want the newest few.
            recent = ids[-limit:] if limit else ids
            # imaplib exposes capabilities as str, but be tolerant of bytes.
            gmail_ext = any(
                "X-GM-EXT-1" in (cap.decode("ascii", "replace") if isinstance(cap, bytes) else cap).upper()
                for cap in (client.capabilities or ())
            )

            out = []
            for msg_id in reversed(recent):
                parts = "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)]"
                if gmail_ext:
                    parts = "(X-GM-MSGID %s)" % parts
                else:
                    parts = "(%s)" % parts
                status, data = client.fetch(msg_id, parts)
                if status != "OK" or not data:
                    continue
                out.append(self._normalise(msg_id, data, gmail_ext))
            return out
        finally:
            _imap_close(client)

    def _normalise(self, msg_id, data, gmail_ext=False):
        raw = b""
        prefix = ""
        for chunk in data:
            if isinstance(chunk, tuple):
                prefix += chunk[0].decode("ascii", "replace")
                raw += chunk[1] or b""
            elif isinstance(chunk, bytes):
                prefix += chunk.decode("ascii", "replace")

        parsed = email.message_from_bytes(raw)
        name, address = _split_address(_decode_header(parsed.get("From", "")))

        received = None
        if parsed.get("Date"):
            try:
                received = email.utils.parsedate_to_datetime(parsed["Date"])
            except (TypeError, ValueError):
                received = None
        if received is not None and received.tzinfo is None:
            # A Date header without an offset is, by convention, local time we
            # cannot resolve. Treating it as UTC at least keeps ordering sane.
            received = received.replace(tzinfo=timezone.utc)

        url = self.web_url
        match = re.search(r"X-GM-MSGID\s+(\d+)", prefix) if gmail_ext else None
        if match:
            # Gmail's web UI addresses messages by the hex form of X-GM-MSGID.
            url = "https://mail.google.com/mail/u/0/#inbox/%x" % int(match.group(1))

        ident = parsed.get("Message-ID") or msg_id.decode("ascii", "replace")
        return {
            "uid": "imap:%s:%s" % (self.id, ident),
            "source": self.id,
            "source_label": self.label,
            "provider": "imap",
            "from_name": _clean(name or address, 60),
            "from_email": _clean(address, 120),
            "subject": _clean(_decode_header(parsed.get("Subject", "")) or "(no subject)", 160),
            # Fetching a body preview would mean decoding MIME parts for every
            # message on every poll; sender and subject carry the dashboard.
            "snippet": "",
            "received": received,
            "unread": True,  # the search was UNSEEN
            "important": False,
            "url": url,
        }


def _imap_close(client):
    """Best-effort teardown. A failure here must not mask a real error."""
    try:
        client.close()
    except Exception:
        pass
    try:
        client.logout()
    except Exception:
        pass


def _decode_header(value):
    """Decode an RFC 2047 header such as '=?UTF-8?B?...?=' into plain text.

    Without this, non-ASCII subjects render as raw encoded-word gibberish,
    which is the single most common IMAP display bug.
    """
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
    except (email.errors.HeaderParseError, ValueError):
        return value
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", "replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(text.decode("utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out).strip()


class IcsAccount:
    """A calendar read from an iCalendar URL — no OAuth, no Cloud project.

    Intended for Google's *⋮ → Settings and sharing → Integrate calendar →
    Secret address in iCal format*, but any reachable `.ics` works.

    The trade against the Calendar API is **freshness**: Google serves that
    export from its own cache, commonly several hours behind, and offers no way
    to force a refresh. Polling faster does not help. That is fine for a
    once-a-day agenda and wrong for "did a meeting just get moved".

    The URL is a bearer credential — anyone holding it can read the calendar —
    so it is treated like a token and never logged.
    """

    provider = "ics"

    def __init__(self, conf):
        self.id = conf["id"]
        self.label = conf.get("label") or conf["id"]
        self.url = conf["url"]
        self.want_calendar = conf.get("calendar", True)
        self.want_mail = False

    def events(self, start, end, tz_name):
        try:
            text = _request("GET", self.url, parse="text")
        except ProviderError as exc:
            # Google 404s the *public* address unless the calendar is published
            # to the world. The bare status code gives no hint of that, and the
            # public and secret addresses sit next to each other in the UI.
            if "HTTP 404" in str(exc) and "/public/" in self.url:
                raise ProviderError(
                    "%s returned 404. This looks like the *public* iCal address, "
                    "which only works if the calendar is published publicly. Use "
                    "'Secret address in iCal format' instead -- it contains "
                    "'/private-' and works while the calendar stays private."
                    % _host(self.url)
                ) from None
            raise
        try:
            components = ics.parse_calendar(text)
        except ics.IcsError as exc:
            raise ProviderError(
                "%s did not return an iCalendar feed (%s). Check the secret "
                "address is the iCal one, not the HTML one." % (_host(self.url), exc)
            ) from None

        raw = ics.expand(components, start.date(), end.date() + timedelta(days=1), tz_name)
        out = []
        for item in raw:
            out.append({
                "uid": "ics:%s:%s" % (self.id, item["uid"]),
                "source": self.id,
                "source_label": self.label,
                "provider": "ics",
                "summary": _clean(item["summary"] or "(no title)", 140),
                "all_day": item["all_day"],
                "raw_start": item["raw_start"],
                "raw_end": item["raw_end"],
                "location": _clean(item["location"], 80),
                "url": item["url"],
                "busy": item["busy"],
            })
        return out

    def unread_count(self):
        return 0

    def messages(self, limit):
        return []


def build_account(conf):
    provider = (conf.get("provider") or "").lower()
    if provider == "google":
        return GoogleAccount(conf)
    if provider in ("microsoft", "outlook", "graph"):
        return MicrosoftAccount(conf)
    if provider == "imap":
        return ImapAccount(conf)
    if provider in ("ics", "ical", "icalendar"):
        return IcsAccount(conf)
    raise ValueError("unknown provider %r for account %r" % (provider, conf.get("id")))


__all__ = [
    "GoogleAccount",
    "IcsAccount",
    "ImapAccount",
    "MicrosoftAccount",
    "ProviderError",
    "build_account",
    "GOOGLE_SCOPES",
    "MS_SCOPES",
]

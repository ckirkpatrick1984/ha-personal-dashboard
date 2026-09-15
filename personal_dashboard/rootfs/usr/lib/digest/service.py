"""The digest sidecar: poll upstreams, serve a flat JSON digest on loopback.

Design notes worth keeping:

* **Credentials never reach the dashboard.** Glance only ever calls 127.0.0.1
  here; the OAuth tokens stay in this process.
* **Stale-on-error.** If an upstream fetch fails, the last good payload is
  served with ``stale: true`` and the error attached. A widget showing slightly
  old events is useful; an empty or errored one is not.
* **The cache holds the parsed result**, not raw bytes, so a restart repaints
  immediately instead of waiting for the first poll.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import normalise
from .providers import ProviderError, build_account

log = logging.getLogger("digest.service")

DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": 8082,
    "timezone": "UTC",
    "poll_seconds": 300,
    "days_ahead": 2,
    "max_events": 25,
    "max_messages_per_account": 10,
    "max_messages": 12,
    "cache_path": "/data/digest-cache.json",
}


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    conf = dict(DEFAULTS)
    conf.update({k: v for k, v in raw.items() if v is not None})
    conf["accounts"] = raw.get("accounts") or []
    return conf


class Digest:
    """Owns the accounts, the poll loop, and the cached payloads."""

    def __init__(self, conf, clock=None):
        self.conf = conf
        self.zone = normalise.get_zone(conf["timezone"])
        # Injectable so tests can pin "now"; every bucketing decision depends
        # on it, and asserting against the real clock is untestable.
        self._clock = clock or (lambda: datetime.now(self.zone))
        self.cache_path = conf["cache_path"]
        self._lock = threading.Lock()
        self._agenda = None
        self._mail = None
        self._stale_since = None
        self._stop = threading.Event()
        # Per-account mail results, so an account with a slower permitted poll
        # rate (Gmail IMAP) can be skipped without blanking the mail widget.
        self._mail_cache = {}
        self._mono = time.monotonic

        self.accounts = []
        for account_conf in conf["accounts"]:
            try:
                self.accounts.append(build_account(account_conf))
            except (KeyError, ValueError) as exc:
                log.error("skipping account %r: %s", account_conf.get("id"), exc)

        self._load_cache()

    # -- cache ------------------------------------------------------------

    def _load_cache(self):
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, ValueError):
            return
        self._agenda = cached.get("agenda")
        self._mail = cached.get("mail")
        self._stale_since = cached.get("generated_at")
        log.info("restored cached digest from %s", self.cache_path)

    def _save_cache(self):
        payload = {
            "generated_at": self._clock().isoformat(),
            "agenda": self._agenda,
            "mail": self._mail,
        }
        tmp = self.cache_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            log.warning("could not write cache: %s", exc)

    # -- polling ----------------------------------------------------------

    def _mail_due(self, account):
        """Whether this account's mail may be fetched on this pass.

        Providers can declare a minimum interval (``min_poll_seconds``). Gmail
        throttles accounts that poll IMAP aggressively, and a lockout lasts
        hours, so the calendar poll interval is deliberately not imposed on it.
        """
        interval = getattr(account, "min_poll_seconds", 0) or 0
        entry = self._mail_cache.get(account.id)
        if interval <= 0 or entry is None:
            return True
        return (self._mono() - entry["at"]) >= interval

    def refresh(self):
        now_local = self._clock()
        start, end = normalise.window(now_local, self.conf["days_ahead"])

        raw_events = []
        raw_messages = []
        unread_by_account = []
        errors = []
        calendar_ok = False
        mail_ok = False

        for account in self.accounts:
            if account.want_calendar:
                try:
                    raw_events.extend(account.events(start, end, self.conf["timezone"]))
                    calendar_ok = True
                except (ProviderError, KeyError, ValueError, TypeError) as exc:
                    log.warning("calendar fetch failed for %s: %s", account.id, exc)
                    errors.append({"account": account.id, "label": account.label,
                                   "scope": "calendar", "message": str(exc)[:200]})

            if account.want_mail:
                if not self._mail_due(account):
                    # Not due yet (Gmail IMAP asks for a gentler cadence than
                    # the calendar poll). Reuse the last good result rather
                    # than dropping this account from the widget.
                    entry = self._mail_cache[account.id]
                    raw_messages.extend(entry["messages"])
                    unread_by_account.append(entry["unread"])
                    mail_ok = True
                    continue

                try:
                    unread = account.unread_count()
                    messages = account.messages(self.conf["max_messages_per_account"])
                    raw_messages.extend(messages)
                    entry = {
                        "id": account.id, "label": account.label,
                        "provider": account.provider, "unread": unread,
                    }
                    unread_by_account.append(entry)
                    self._mail_cache[account.id] = {
                        "at": self._mono(), "messages": messages, "unread": entry,
                    }
                    mail_ok = True
                except (ProviderError, KeyError, ValueError, TypeError) as exc:
                    log.warning("mail fetch failed for %s: %s", account.id, exc)
                    errors.append({"account": account.id, "label": account.label,
                                   "scope": "mail", "message": str(exc)[:200]})

        calendar_errors = [e for e in errors if e["scope"] == "calendar"]
        mail_errors = [e for e in errors if e["scope"] == "mail"]

        # An account that deliberately does not do mail must not make the mail
        # side look broken, so "nobody asked" counts as success.
        if not any(a.want_calendar for a in self.accounts):
            calendar_ok = True
        if not any(a.want_mail for a in self.accounts):
            mail_ok = True

        # Calendar and mail are kept independent. If every calendar fetch failed
        # we must not rebuild the agenda from an empty list -- that would replace
        # real cached events with a confident, wrong "Clear day". The same
        # applies to mail and a false "Inbox zero". Each side either refreshes
        # or goes stale on its own.
        keep_agenda = bool(self.accounts) and not calendar_ok and self._agenda is not None
        keep_mail = bool(self.accounts) and not mail_ok and self._mail is not None

        events = normalise.normalise_events(
            raw_events, self.zone, now_local, self.conf["days_ahead"]
        )[: self.conf["max_events"]]
        messages = normalise.normalise_messages(
            raw_messages, self.zone, now_local, self.conf["max_messages"]
        )

        generated = now_local.isoformat()
        with self._lock:
            if keep_agenda:
                self._go_stale(self._agenda, calendar_errors)
            else:
                self._agenda = {
                    "generated_at": generated,
                    "timezone": self.conf["timezone"],
                    "stale": False,
                    "stale_since": None,
                    "errors": calendar_errors,
                    "summary": normalise.summarise_events(events, now_local),
                    "groups": normalise.group_events(events),
                    "events": events,
                }

            if keep_mail:
                self._go_stale(self._mail, mail_errors)
            else:
                self._mail = {
                    "generated_at": generated,
                    "timezone": self.conf["timezone"],
                    "stale": False,
                    "stale_since": None,
                    "errors": mail_errors,
                    "summary": normalise.summarise_messages(messages, unread_by_account),
                    "messages": messages,
                }

            self._stale_since = generated if (keep_agenda or keep_mail) else None
            self._save_cache()

        return not (keep_agenda and keep_mail)

    @staticmethod
    def _go_stale(payload, errors):
        """Keep serving this payload, but say plainly that it is old."""
        if payload is None:
            return
        if not payload.get("stale"):
            payload["stale_since"] = payload.get("generated_at")
        payload["stale"] = True
        payload["errors"] = errors

    def run_forever(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # never let the poll thread die
                log.exception("unexpected error during refresh")
            # Jitter keeps this off the round minute, where every other
            # scheduled job on the internet already is.
            delay = self.conf["poll_seconds"] * (0.9 + random.random() * 0.2)
            self._stop.wait(delay)

    def stop(self):
        self._stop.set()

    # -- reads ------------------------------------------------------------

    def agenda(self):
        with self._lock:
            return self._agenda or self._empty("events")

    def mail(self):
        with self._lock:
            return self._mail or self._empty("messages")

    def health(self):
        with self._lock:
            return {
                "ok": self._agenda is not None or self._mail is not None,
                "accounts": [{"id": a.id, "label": a.label, "provider": a.provider}
                             for a in self.accounts],
                "stale_since": self._stale_since,
                "timezone": self.conf["timezone"],
                "poll_seconds": self.conf["poll_seconds"],
            }

    def _empty(self, key):
        """Served before the first successful poll: an honest, renderable shape."""
        base = {
            "generated_at": None,
            "timezone": self.conf["timezone"],
            "stale": True,
            "stale_since": None,
            "errors": [],
            key: [],
        }
        if key == "events":
            base["groups"] = []
            base["summary"] = {
                "today_count": 0, "tomorrow_count": 0, "total_count": 0,
                "busy_minutes_today": 0, "current": None, "next": None,
                "headline": "Waiting for first sync" if self.accounts else "No accounts configured",
            }
        else:
            base["summary"] = {
                "unread_total": 0, "important_count": 0, "shown_count": 0,
                "by_account": [],
                "headline": "Waiting for first sync" if self.accounts else "No accounts configured",
            }
        return base


class Handler(BaseHTTPRequestHandler):
    server_version = "digest/1.0"
    digest = None

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        routes = {
            "/agenda": self.digest.agenda,
            "/mail": self.digest.mail,
            "/health": self.digest.health,
        }
        path = self.path.split("?", 1)[0].rstrip("/") or "/health"
        handler = routes.get(path)
        if handler is None:
            self._send(404, {"error": "not found", "routes": sorted(routes)})
            return
        try:
            self._send(200, handler())
        except Exception:
            log.exception("error serving %s", path)
            self._send(500, {"error": "internal error"})

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


def serve(conf):
    digest = Digest(conf)

    poller = threading.Thread(target=digest.run_forever, name="digest-poll", daemon=True)
    poller.start()

    handler = type("BoundHandler", (Handler,), {"digest": digest})
    httpd = ThreadingHTTPServer((conf["listen_host"], int(conf["listen_port"])), handler)
    log.info(
        "digest listening on %s:%s with %d account(s), polling every %ss",
        conf["listen_host"], conf["listen_port"], len(digest.accounts), conf["poll_seconds"],
    )
    try:
        httpd.serve_forever()
    finally:
        digest.stop()
        httpd.server_close()

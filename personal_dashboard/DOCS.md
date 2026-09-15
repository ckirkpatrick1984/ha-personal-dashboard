# Personal Dashboard

Glance (dashboard) plus Vikunja (tasks), running as one add-on.

After starting, two web interfaces are available on your Home Assistant host:

| Port | What |
|---|---|
| 8080 | The dashboard — this is the page you open every morning |
| 3456 | Vikunja — where you manage tasks in detail |

## First-time setup

The order matters, because the dashboard needs an API token that does not
exist until you have created an account.

### 1. Set `vikunja_public_url` before starting

This is the single most important option. Vikunja's web interface builds its
API address from this value. **It must be exactly the URL you type into your
browser**, including the port and a trailing slash.

If you reach Home Assistant over Tailscale at `homeassistant`, use:

```
http://homeassistant:3456/
```

If you use a plain IP address, use that instead:

```
http://192.168.1.50:3456/
```

> **Tailscale users:** use your machine's MagicDNS name, not its `100.x.y.z`
> address. Home Assistant itself refuses to log in over a Tailscale IP
> ("Invalid client id"), because `100.64.0.0/10` is not in the list of local
> networks its OAuth check accepts. Vikunja has no such restriction, but
> sticking to one hostname everywhere avoids confusion.

Getting this wrong does not produce a helpful error. The Vikunja login page
will load normally and then fail with a generic **"network error"** when you
try to log in or register, because the browser blocks the cross-origin
request. If that happens, correct this option and restart the add-on.

### 2. Start the add-on and create your account

Open `http://<your-host>:3456/` and register. The first account you create is
yours; there is no default login.

The task widget on the dashboard will show an error until step 3 — that is
expected.

### 3. Create an API token and paste it into the options

In Vikunja: **avatar menu → Settings → API Tokens → Create a token**.

Give it at least **read access to Tasks**. Copy the token — Vikunja shows it
only once — then paste it into the add-on's `vikunja_token` option and
**restart the add-on**.

The dashboard's task list will now populate.

### 4. Turn off registration (optional but recommended)

Once your account exists, nobody else needs to create one. This add-on leaves
registration enabled so you can get set up; if you expose these ports beyond a
private network, consider that carefully.

## Options

| Option | Required | Description |
|---|---|---|
| `vikunja_public_url` | **yes** | The exact URL you use to reach Vikunja, with port and trailing slash. See above. |
| `weather_location` | no | A place name Glance can geocode, e.g. `Asheville, North Carolina, United States`. |
| `home_assistant_url` | no | Used for the Home Assistant bookmark and the service monitor. |
| `vikunja_token` | no | Vikunja API token for the task widget. Blank until you complete step 3. |
| `timezone` | no | Overrides the timezone. Leave blank to inherit Home Assistant's. |
| `google_label` | no | Label shown beside personal events and mail. Default `Personal`. |
| `ics_url` | no | Secret iCal URL for a Google calendar. No Cloud project needed. Treat as a password. |
| `ics_label` | no | Name shown beside events from that feed. Default `Personal`. |
| `google_client_id` | no | OAuth client ID (Desktop app) for the personal Google account. |
| `google_client_secret` | no | OAuth client secret for the same client. |
| `google_refresh_token` | no | Refresh token from `authorize-google.py`. |
| `google_calendar_ids` | no | Calendar IDs to read. Default `primary`. |
| `google_enable_mail` | no | Read Gmail too. Set `false` for a calendar-only token. Default `true`. |
| `imap_label` | no | Display name for the IMAP account. Default `Gmail`. |
| `imap_host` | no | IMAP server. Default `imap.gmail.com`. |
| `imap_username` | no | Full email address for IMAP mail. |
| `imap_password` | no | App password (needs 2-Step Verification), not your account password. |
| `imap_poll_minutes` | no | Minutes between IMAP checks. Default `10`, min `5` (Gmail throttles frequent polling). |
| `microsoft_label` | no | Label shown beside work events and mail. Default `Work`. |
| `microsoft_client_id` | no | Entra application (client) ID. |
| `microsoft_tenant` | no | Directory (tenant) ID, or `common`. |
| `microsoft_refresh_token` | no | Refresh token from `authorize-microsoft.py`. |
| `microsoft_enable_mail` | no | Read Outlook mail too. Default `true`. |
| `microsoft_focused_only` | no | Only show the Focused inbox, not Other. Default `true`. |
| `digest_poll_minutes` | no | How often to refresh calendars and Graph mail. 1–60, default 5. Gmail over IMAP uses `imap_poll_minutes` instead. |
| `digest_days_ahead` | no | How far ahead the agenda looks. 1–14, default 2. |
| `rss_feeds` | no | A list of feed URLs shown in the News widget. |
| `log_level` | no | `debug`, `info`, `warning` or `error`. |

### 5. Connect calendar and mail (optional)

The Agenda and Inbox widgets read a sidecar service that talks to Google
Calendar, Gmail/IMAP and Microsoft Graph. Until you configure an account they
show a setup prompt, and the rest of the dashboard works normally.

**Recommended path for a personal Google account: no Google Cloud project at
all.** Take the calendar from its iCalendar URL and the mail over IMAP:

1. **Calendar** — in Google Calendar on desktop web, hover your calendar in the
   left sidebar under *My calendars*, click **⋮ → Settings and sharing**, scroll
   to **Integrate calendar**, and copy **Secret address in iCal format** into
   `ics_url`. It contains `/private-` and ends in `/basic.ics` — the *public*
   address and the *embed code* will not work. Treat the URL as a password;
   **Reset** on that page revokes it.
2. **Mail** — enable 2-Step Verification, create an app password at
   <https://myaccount.google.com/apppasswords>, set `imap_username` and
   `imap_password`.

The cost is freshness: Google caches that export for several hours, so it suits
a once-a-day agenda rather than "has this meeting just moved". Recurring events
are expanded by the add-on.

**If you need a minute-fresh agenda**, use the Calendar API instead of
`ics_url`. That means a Cloud project, consent screen and OAuth client. Mail
still goes over IMAP — Gmail's API scope is classed *restricted* by Google, a
much higher bar than Calendar's *sensitive*, and an app password avoids it.

Authorisation then happens on a machine with a browser, not here:

```bash
# Calendar only -- then set google_enable_mail: false and fill in imap_*
./scripts/authorize-google.py --calendar-only --client-id XXXX --client-secret YYYY

# Work account: Graph is the only option, device code flow works over SSH
./scripts/authorize-microsoft.py --client-id XXXX --tenant <tenant-id>
```

Drop `--calendar-only` if you would rather use one Google credential for both
and are willing to deal with the restricted scope.

For Gmail's app password: enable 2-Step Verification, then create one at
<https://myaccount.google.com/apppasswords> and set `imap_username` and
`imap_password`.

Each script prints the options to paste in. An account activates only when all
of its credentials are present. Full instructions, including the Google consent
screen's 7-day refresh token trap and the work-tenant restrictions that can
block Microsoft entirely, are in `docs/calendar-mail.md`.

The sidecar listens on `127.0.0.1:8082` inside the container only. It is
deliberately **not** a published port: the OAuth refresh tokens live in that
process.

## Where your data lives

Everything is in the add-on's own volume:

```
/data/vikunja.db        your tasks
/data/files             attachments
/data/.service_secret   session signing key (generated once)
/data/vikunja.yml       generated from options - edits are overwritten
/data/glance.yml        generated from options - edits are overwritten
/data/digest-config.json  calendar/mail credentials - generated, chmod 600
/data/digest-cache.json   last good agenda and inbox, for stale-on-error
```

This volume is included in Home Assistant backups — **but only if backups
actually run**. Check **Settings → System → Backups** and configure an
automatic schedule before you rely on this for real task data.

## Networking notes

The ports are published on the Home Assistant host, which means they are
reachable on every address that host has — including its Tailscale address if
you run the Tailscale add-on. They are **not** protected by Home Assistant
login. Do not forward these ports through your router.

## Troubleshooting

**"Network error" when logging in to Vikunja** — `vikunja_public_url` does not
match the URL in your address bar. See step 1.

**Task widget shows "Vikunja rejected the API token"** — the token is missing,
wrong, or lacks read access to Tasks. Recreate it and restart the add-on.

**Task widget shows a status code** — check the add-on log. A `400` usually
means the API path is wrong; a `502` means Vikunja is not running.

**Agenda or Inbox shows "No calendar connected yet"** — no complete set of
credentials in the options. The add-on log prints how many accounts it built at
startup.

**Agenda worked for a week, then stopped** — the Google OAuth consent screen is
still in *Testing*, so the refresh token expired after 7 days. Publish the app
and re-authorise.

**One account is missing but the other works** — the widget names the failing
account. Usually a revoked or expired token; check the add-on log for the
provider's error.

**Add-on will not start** — check the log. If the build failed on a checksum,
the upstream release was modified and the add-on needs updating.

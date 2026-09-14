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
| `rss_feeds` | no | A list of feed URLs shown in the News widget. |
| `log_level` | no | `debug`, `info`, `warning` or `error`. |

## Where your data lives

Everything is in the add-on's own volume:

```
/data/vikunja.db        your tasks
/data/files             attachments
/data/.service_secret   session signing key (generated once)
/data/vikunja.yml       generated from options - edits are overwritten
/data/glance.yml        generated from options - edits are overwritten
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

**Add-on will not start** — check the log. If the build failed on a checksum,
the upstream release was modified and the add-on needs updating.

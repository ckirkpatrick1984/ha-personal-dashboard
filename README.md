# Personal Dashboard — Home Assistant Add-on Repository

A Home Assistant add-on that runs a personal daily dashboard alongside Home
Assistant, without modifying anything in Home Assistant itself.

It bundles two open-source projects:

- **[Glance](https://github.com/glanceapp/glance)** — the dashboard: clock,
  weather, calendar, RSS, bookmarks, service status, and a live task list.
- **[Vikunja](https://vikunja.io/)** — the task manager behind that list:
  projects, sub-tasks, dependencies, recurring tasks, reminders, labels,
  priorities, Kanban, Gantt and saved filters.

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Open the **⋮** menu (top right) → **Repositories**.
3. Add this repository's URL.
4. Find **Personal Dashboard** in the store and click **Install**.
5. Configure it (see the add-on's Documentation tab), then **Start**.

The first build downloads two binaries (about 75 MB total) and takes a few
minutes on a Raspberry Pi.

## What it does not do

This add-on is deliberately unprivileged. It declares:

```yaml
hassio_api: false
homeassistant_api: false
auth_api: false
map: []
```

It cannot read or write your Home Assistant configuration, cannot call the
Home Assistant API, and cannot see your entities. It stores its own data in
its own add-on volume, which is included in Home Assistant backups.

## Supported architectures

`aarch64` (Raspberry Pi 4/5, 64-bit) and `amd64`.

## Licence

The add-on packaging in this repository is MIT licensed. Glance and Vikunja
are distributed under their own licences.

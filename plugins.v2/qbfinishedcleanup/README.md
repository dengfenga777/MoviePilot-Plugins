# QbFinishedCleanup

MoviePilot plugin: qB已整理自动清理。

This plugin deletes qBittorrent tasks with a specific tag that have seeded for at least the configured number of days, and removes their local files.

## Behavior

- qBittorrent only.
- Default tag: `已整理`.
- Default minimum seeding time: `3` days.
- Deletes torrent tasks with `delete_file=True`.
- Only completed tasks that have seeded at least the configured days are deleted by default.
- All matching tasks are deleted in one run.
- The plugin is disabled by default.

## Install

Copy this directory to MoviePilot's plugin path:

```bash
/app/app/plugins/qbfinishedcleanup
```

For the current Docker setup on ovh, the host mount target is:

```bash
/opt/moviepilot/custom_plugins/qbfinishedcleanup
```

Then restart `moviepilot-v2` or reload plugins from MoviePilot.

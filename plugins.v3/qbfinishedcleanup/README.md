# QbFinishedCleanup

MoviePilot V3 plugin: qB已整理自动清理。

This plugin deletes qBittorrent tasks with a specific tag that have seeded for at least the configured number of days, and removes their local files.

## Behavior

- qBittorrent only.
- Default tag: `已整理`.
- Default minimum seeding time: `3` days.
- Deletes torrent tasks with `delete_file=True`.
- Only completed tasks that have seeded at least the configured days are deleted by default.
- All matching tasks are deleted in one run.
- The plugin is disabled by default.
- Uses MoviePilot V3 stable SDK imports and requires MoviePilot 3.0.0 or newer.

## Install

Copy this directory to MoviePilot's plugin path:

```bash
/app/app/plugins/qbfinishedcleanup
```

For the current Docker setup, mount the host plugin directory to:

```bash
/opt/moviepilot/custom_plugins/qbfinishedcleanup
```

Then restart MoviePilot V3 or reload plugins from MoviePilot.

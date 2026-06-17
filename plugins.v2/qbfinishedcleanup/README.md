# QbFinishedCleanup

MoviePilot plugin: qB已整理自动清理。

When disk free space drops below the configured threshold, this plugin deletes qBittorrent tasks with a specific tag that have been seeding longer than the configured number of days, and removes their local files.

## Behavior

- qBittorrent only.
- Default tag: `已整理`.
- Default threshold: `500` GB free space.
- Default minimum seeding time: `3` days.
- Deletes torrent tasks with `delete_file=True`.
- Only completed tasks that have seeded longer than the configured days are deleted by default.
- A per-run delete limit is available to avoid deleting too much at once.
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

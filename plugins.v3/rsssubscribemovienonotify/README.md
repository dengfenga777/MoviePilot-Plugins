# RssSubscribeMovieNoNotify

MoviePilot plugin: 电影订阅无通知。

This MoviePilot V3 plugin is based on `RssSubscribeNoNotify` and is intended for movie RSS feeds. It keeps RSS matching, subscribe, download, history, and scheduling behavior, but only processes recognized movies and suppresses notifications.

## Changes

- Plugin class: `RssSubscribeMovieNoNotify`
- Plugin name: `电影订阅无通知`
- Config prefix: `rsssubscribemovienonotify_`
- Default `notify` value: `false`
- Only recognized `电影` items are processed; TV/anime items are skipped.
- Default action is direct download. The action can still be changed to subscribe in the plugin form.
- MoviePilot subscription priority rules are enabled by default.
- Rule group usage and match results are logged for each RSS candidate.
- The visible notification switch is removed from the config form.
- Validation errors are logged only and are not sent through `systemmessage`.
- Subscription creation passes `message=False`.
- Download actions use a silent `DownloadChain` subclass to suppress MoviePilot's default download notifications.
- Empty save paths are passed as automatic download paths, matching MoviePilot's built-in behavior.
- One failed RSS feed does not stop later feeds from being processed.
- Uses the MoviePilot V3 stable SDK and unified `media_source + media_id` identity contract.
- Existing history entries containing `tmdbid` are migrated automatically and idempotently.
- Overlapping scheduled/manual runs are skipped to prevent duplicate processing.
- A failed RSS source or item no longer interrupts the remaining sources and items.
- Subscription results are checked before history is recorded.
- RSS links use stable fingerprints for deduplication and history is capped at 1000 entries.
- History deletion uses Bearer authentication and a precise record ID.

## Install

Copy this directory to MoviePilot's plugin path:

```bash
/app/app/plugins/rsssubscribemovienonotify
```

For the current Docker setup, mount the host plugin directory to:

```bash
/opt/moviepilot/custom_plugins/rsssubscribemovienonotify
```

Then restart MoviePilot V3 or reload plugins from MoviePilot.

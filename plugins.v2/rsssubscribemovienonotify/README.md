# RssSubscribeMovieNoNotify

MoviePilot plugin: 电影订阅无通知。

This plugin is based on `RssSubscribeNoNotify` and is intended for movie RSS feeds. It keeps RSS matching, subscribe, download, history, and scheduling behavior, but only processes recognized movies and suppresses notifications.

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

## Install

Copy this directory to MoviePilot's plugin path:

```bash
/app/app/plugins/rsssubscribemovienonotify
```

For the current Docker setup, mount the host plugin directory to:

```bash
/opt/moviepilot/custom_plugins/rsssubscribemovienonotify
```

Then restart `moviepilot-v2` or reload plugins from MoviePilot.

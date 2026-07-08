# RssSubscribeNoNotify

MoviePilot plugin: 自定义订阅无通知。

This plugin is based on MoviePilot's built-in `RssSubscribe` plugin for the currently deployed `jxxghp/moviepilot-v2:latest` image. It keeps the RSS matching, subscribe, download, history, and scheduling behavior, but removes notification behavior.

## Changes

- Plugin class: `RssSubscribeNoNotify`
- Plugin name: `自定义订阅无通知`
- Config prefix: `rsssubscribenonotify_`
- Default `notify` value: `false`
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
/app/app/plugins/rsssubscribenonotify
```

For the current Docker setup, mount the host plugin directory to:

```bash
/opt/moviepilot/custom_plugins/rsssubscribenonotify
```

Then restart `moviepilot-v2` or reload plugins from MoviePilot.

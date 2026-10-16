# RssSubscribeMovieNoNotify

MoviePilot plugin: 电影订阅无通知。

This MoviePilot V3 plugin is based on `RssSubscribeNoNotify` and is intended for movie RSS feeds. It keeps RSS matching, subscribe, download, history, and scheduling behavior, but only processes recognized movies and suppresses notifications.

## 2.3.0：媒体库已有的电影也能升级

- 媒体库已有这部电影时，查 Emby/Jellyfin 里已有版本的分辨率（strm 没有视频流信息，就看文件名里的 2160p/1080p）。
- 已有版本低于 RSS 里的版本（比如库里 1080p、RSS 出了 2160p），就升级一次；已有版本算一次下载，之后不再升级。
- 已有版本中有认不出分辨率的，或查询失败，就不升级，避免白下。

## 2.2.0：同一部电影只下一个版本

- 先读完所有 RSS 源，再按电影分组，每部电影只下载最好的一个版本：分辨率高的优先（2160p > 1080p），同分辨率体积大（码率高）的优先，再按 RSS 源填写顺序。
- 下载失败会自动换下一个版本。
- 之后出现更高分辨率（比如先下了 1080p、后来出了 2160p）只升级一次，每部电影最多下载 2 次。
- 媒体库已有且不是本插件下载过的电影，照旧跳过。
- 日志里隐藏 passkey、rsskey 等密钥。

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

# RSS 优选下载插件仓库

这是一个用于 `MoviePilot V2` 的第三方插件仓库，当前仅包含一个插件：

- `RSS优选下载`

## 功能

- 读取 PT 站 RSS 条目
- 使用 MoviePilot 自身识别能力识别 `TMDB / 季 / 集`
- 同一集同时出现多个版本时，优先下载更高质量版本
- 默认优先级：`2160p/4K > 1080p > 720p > 其他`
- 同分辨率下可优先 `HEVC/H.265`
- 如果后续刷新又出现同一集，并且新资源体积更大，会再次推送下载
- 默认过滤整季/完结包，例如 `Complete`、`全集`、`全季`、`完结`

## 仓库结构

```text
package.v2.json
plugins.v2/
  rssbestversion/
    __init__.py
```

## 当前版本

- `RssBestVersion` `v1.1`

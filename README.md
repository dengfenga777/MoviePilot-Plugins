# MoviePilot 插件仓库

这是一个用于 `MoviePilot V2` 的第三方插件仓库，当前只保留一个插件：

- `聚合RSS优选下载`

## 聚合RSS优选下载

- 面向多 RSS 源场景，先把多条 PT 站 RSS 聚合成统一候选池
- 聚合层会并发拉取 RSS，按 `guid / enclosure / link / title+size` 去重
- 聚合结果会按发布时间倒序缓存，默认只保留最近 `200` 条
- 可在聚合层先粗过滤 `CAM / TS / TC / 720p / 合集 / Complete / Pack` 等明显不需要的资源
- 可通过插件 API `/unified_rss` 输出统一 RSS；配置 `feed_token` 后访问时需要带 `?token=...`
- 插件主逻辑仍交给 MoviePilot 识别 `TMDB / 季 / 集`
- 同一集多版本会按质量、站点优先级、编码、体积和发布时间择优
- 默认允许已入库电视剧单集继续进入候选，用于 RSS 单集洗版
- `臻彩 / 真彩 / 高动态` 会按 `4K HDR` 处理，`60fps / 高帧率` 会作为同等级优选项
- 下载下发保持串行，并保留历史判重，避免同集重复下发
- 下载链通知已静音，不会触发 MoviePilot 默认同步通知

## 仓库结构

```text
package.v2.json
plugins.v2/
  rssaggregatebestversion/
    __init__.py
```

## 当前版本

- `RssAggregateBestVersion` `v1.0.2`

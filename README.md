# MoviePilot 插件仓库

这是一个用于 `MoviePilot V2` 的第三方插件仓库。

- `整理记录单集洗版`
- `聚合RSS优选下载`

## 整理记录单集洗版

- 不读取 RSS，不扫描媒体库，只读取 MoviePilot 最近成功的整理记录
- 只处理电视剧单集，按 `tmdb_id + SxxExx` 去重
- 每个单集调用 MoviePilot 自带搜索链路，继续使用 MP 的站点搜索、识别和过滤规则
- 搜索结果必须精确匹配同一季同一集，整季包、多集包不会作为洗版候选
- 用整理记录里的源路径、目标路径和文件清单判断当前版本质量
- 支持 `4K HDR / 臻彩 / 真彩 / 高动态 / 2160p / 1080p / 720p` 等质量分级
- 支持 `60fps / 高帧率 / 100fps / 120fps` 作为同质量优选项
- 同质量下可继续按 `HEVC/H.265`、体积等维度判断是否更优
- 每轮最多搜索指定数量的单集，并对已搜索单集设置冷却，避免反复全量搜索
- 下发下载时静音 MoviePilot 下载链默认通知，只发送插件自己的汇总通知

## 聚合RSS优选下载

- 面向多 RSS 源场景，先把多条 PT 站 RSS 聚合成统一候选池
- 聚合层会并发拉取 RSS，按 `guid / enclosure / link / title+size` 去重
- 默认每轮最多识别 `40` 条新增 RSS item，并跳过已评估过的旧 item，避免整轮反复识别老资源
- 聚合结果会按发布时间倒序缓存，默认只保留最近 `200` 条
- 可在聚合层先粗过滤 `CAM / TS / TC / 720p / 合集 / Complete / Pack` 等明显不需要的资源
- 可通过插件 API `/unified_rss` 输出统一 RSS；配置 `feed_token` 后访问时需要带 `?token=...`
- 插件主逻辑仍交给 MoviePilot 识别 `TMDB / 季 / 集`
- 同一集多版本会按质量、站点优先级、编码、体积和发布时间择优
- 默认允许已入库电视剧单集继续进入候选，用于 RSS 单集洗版
- `臻彩 / 真彩 / 高动态` 会按 `4K HDR` 处理，`60fps / 高帧率` 会作为同等级优选项
- 发布时间只参与本轮排序，不会单独触发洗版；避免同质量新发资源反复重下
- 下载下发保持串行，并保留历史判重，避免同集重复下发
- 历史页会显示优选分、体积、推送次数和原始种子标题，方便排查洗版选择
- 会兼容 M-Team RSS 的 `dlv2` 下载链接，避免下载器拿到 JSON 导致种子解析失败
- 会尝试修复 RSS 标题编码错乱，减少中文标题乱码
- 下载链通知已静音，不会触发 MoviePilot 默认同步通知

## 仓库结构

```text
package.v2.json
plugins.v2/
  singleepisodeupgrade/
    __init__.py
  rssaggregatebestversion/
    __init__.py
```

## 当前版本

- `SingleEpisodeUpgrade` `v1.0.0`
- `RssAggregateBestVersion` `v1.1.0`

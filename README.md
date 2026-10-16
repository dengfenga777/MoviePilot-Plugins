# MoviePilot 插件仓库

这里维护 `MoviePilot V2 / V3` 第三方插件：

- `自定义订阅无通知`
- `电影订阅无通知`
- `qB已整理自动清理`
- `RSS剧集优选`（V3）
- `TG群组智能补集`（V3）

## 自定义订阅无通知

基于 MoviePilot 内置 `自定义订阅` 插件重建。

- 定时刷新 RSS 报文
- 默认接入 MoviePilot 订阅优先级规则
- 记录订阅规则组使用和命中情况
- 支持包含、排除、代理规则
- 支持添加订阅或直接下载
- 支持保存历史记录和清理历史记录
- 默认不发送通知
- 配置页不显示“发送通知”开关
- 错误只写日志，不发送系统消息
- 订阅添加时传入 `message=False`
- 下载链使用静音子类，屏蔽 MoviePilot 默认下载成功/失败通知

## 仓库结构

```text
package.v2.json
package.v3.json
plugins.v2/
  qbfinishedcleanup/
    __init__.py
    README.md
  rsssubscribenonotify/
    __init__.py
    README.md
  rsssubscribemovienonotify/
    __init__.py
    README.md
plugins.v3/
  qbfinishedcleanup/
  rsssubscribenonotify/
  rsssubscribemovienonotify/
  rssepisodequality/
  tgepisoderequest/
```

## 电影订阅无通知

基于通用无通知 RSS 插件拆出的电影专用版本。

- 只处理识别为电影的 RSS 项
- 非电影内容会记录日志并跳过
- 默认动作是直接下载
- 仍可在配置页改成添加订阅
- 不发送系统通知
- 保存目录留空时走 MoviePilot 自动目录

## qB已整理自动清理

清理 qB 中指定标签且保种达到指定天数的任务。

- 默认标签 `已整理`
- 默认最少保种 `3` 天
- 删除 qB 任务和本地文件
- 默认只处理已完成任务
- 支持试运行
- 达到条件的任务会在一次运行中全部删除
- 插件默认关闭

## MoviePilot V3 适配

- V2 实现保持在 `plugins.v2/`，不影响旧版用户。
- V3 实现位于 `plugins.v3/`，并使用 `package.v3.json` 独立发布。
- V3 代码已迁移到稳定 `app.sdk` 导入。
- 两个 RSS 插件已改用 `media_source + media_id` 统一媒体身份，并会幂等迁移旧历史中的 `tmdbid`。
- RSS 检查具备执行锁、单源异常隔离、订阅结果校验、稳定指纹去重和有界历史。
- V3 历史删除接口使用 Bearer 鉴权，不再在页面参数中携带 API Token。
- V3 插件最低要求 `MoviePilot >= 3.0.0`。

## 当前版本

- `RssSubscribeNoNotify`：V3 `v3.1.0` / V2 `v2.1.5`
- `RssSubscribeMovieNoNotify`：V3 `v2.1.0` / V2 `v1.0.1`
- `QbFinishedCleanup`：V3 `v2.0.0` / V2 `v1.0.3`

## RSS剧集优选（V3）

独立插件 `RssEpisodeQuality`，当前版本 `v1.3.0`，要求 MoviePilot `>= 3.0.0`。

汇总多个 RSS 源后按现有订阅规则优选剧集，严格查询媒体库完整分页并合并已有版本。默认只补缺集，更新包通过 qBittorrent 暂停、选集和复核后仅下载目标集数；查询或选集失败时暂缓，不整包回退。可配置按实际版本信息升级，保存待完成任务与下载决策，不发送通知。

启用本插件前，请停用负责同一批剧集的旧 RSS 下载任务，再将 RSS 地址复制到本插件；独立插件之间不共享下载质量记录。插件默认关闭，不会自动修改其他插件。

详细配置与处理边界见 [插件说明](plugins.v3/rssepisodequality/README.md)。验证与测试命令见 [测试说明](tests/rss_episode_quality/VALIDATION.md)。

## TG群组智能补集（V3）

独立插件 `TgEpisodeRequest`，当前版本 `v1.3.2`，要求 MoviePilot `>= 3.0.0`。

插件内运行一个独立的 Telegram 机器人。群成员发「tmdb 12345 缺第5集」「tmdb 12345 S02E05 资源有问题」「tmdb 550 电影 资源有问题」，插件识别 TMDB ID、季、集和意图，调用 MoviePilot 精确搜索，按「画质规则 + 做种数 + 免费」排序后默认自动下载最优资源，失败自动换下一个（也可改为点按钮选择）。入库后在群里 @ 求片的人，处理完的消息会自动删除；可选对接 PMS 积分（电影按部、剧集按季收费，支持每月免费额度）。整季包只勾选请求的集。

详见 [插件说明](plugins.v3/tgepisoderequest/README.md)。

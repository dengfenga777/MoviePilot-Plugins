# Emby媒体库封面生成（异步版）

基于 Kioo 的 [MediaCoverGenerator](https://github.com/wio-ki/MoviePilot-Plugins) 1.0.1 修改。

## 和原版的区别

- 整理完成（TransferComplete）和 Webhook `library.new` 事件只登记到后台队列，处理函数立即返回，不会拖住 MoviePilot 的整理完成事件
- 「入库延迟」变成队列里的等待时间，不再在事件线程里 `sleep`
- 媒体服务器里还查不到媒体时，每 5 分钟重试，最多 6 次，不再调用媒体库全量同步
- 同一媒体去重；同一媒体库 2 分钟内合并成一次封面生成
- 定时全量更新、手动更新、样式、字体、历史记录等功能和原版相同

## 迁移

1. 关闭原版插件，至少关掉它的「入库监控」
2. 安装本插件。首次安装会沿用原版配置，但不会自动启用
3. 检查配置后启用

插件数据目录是 `/config/plugins/MediaCoverGeneratorAsync`，字体会重新下载一次。

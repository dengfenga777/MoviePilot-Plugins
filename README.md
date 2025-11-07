# MoviePilot Plugin - StrmWebhook

## 功能说明
当 MoviePilot 整理 / 入库 完成后，
本插件会自动向你设置的 Webhook URL 发送一个 JSON 通知。

你可以在另一台机器上运行接收脚本（例如 Python Flask 服务），
根据接收到的媒体路径生成 `.strm` 文件，实现分布式入库与播放。

# MoviePilot 插件仓库

这里维护 `MoviePilot V2` 第三方插件：

- `自定义订阅无通知`
- `qB已整理自动清理`

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
plugins.v2/
  qbfinishedcleanup/
    __init__.py
    README.md
  rsssubscribenonotify/
    __init__.py
    README.md
```

## qB已整理自动清理

磁盘剩余空间低于阈值时，清理 qB 中指定标签的已完成任务。

- 默认阈值 500GB
- 默认标签 `已整理`
- 删除 qB 任务和本地文件
- 默认只处理已完成任务
- 支持单次删除数量限制
- 插件默认关闭

## 当前版本

- `RssSubscribeNoNotify` `v2.1.4`
- `QbFinishedCleanup` `v1.0.0`

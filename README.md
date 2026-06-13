# MoviePilot 插件仓库

这里只保留一个 `MoviePilot V2` 第三方插件：

- `自定义订阅无通知`

## 自定义订阅无通知

基于 MoviePilot 内置 `自定义订阅` 插件重建。

- 定时刷新 RSS 报文
- 支持包含、排除、代理、订阅优先级规则
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
  rsssubscribenonotify/
    __init__.py
    README.md
```

## 当前版本

- `RssSubscribeNoNotify` `v2.1.2`

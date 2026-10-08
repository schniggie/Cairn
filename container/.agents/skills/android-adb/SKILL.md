---
name: android-adb
description: 需要操作安卓设备或模拟器时使用：adb 设备管理、安装/提取 APK、shell 执行、日志抓取、端口转发；目标涉及移动 App 分析或已连接的安卓设备时想到它
---

## 用法

* 先 `adb devices` 确认设备在线；多设备时所有命令加 `-s <serial>`
* logcat 必须用 `-d`（dump 后退出）或 `timeout` 限时，否则会挂住执行片
* 提取的 APK、日志、截图统一落盘 `mobile/`

确认设备连接状态：

```bash
adb devices -l
```

列出第三方应用包名（定位目标 App）：

```bash
adb shell pm list packages -3 | tee mobile/packages.txt
```

提取目标 APK 到工作目录（先 `adb shell pm path <包名>` 拿路径）：

```bash
adb pull /data/app/<包名>/base.apk mobile/
```

抓取一次日志（dump 模式，抓完即退）：

```bash
adb logcat -d -v time > mobile/logcat.txt
```

设备端口转发（把设备 8080 映射到本机，配合 frida/调试服务）：

```bash
adb forward tcp:8080 tcp:8080 && adb forward --list | tee mobile/forwards.txt
```

## 规则

- 设备操作前先确认目标设备归属与授权，多设备环境显式 `-s` 指定，避免误操作他人设备
- 禁止长驻：`adb logcat`（无 -d）、`adb shell` 交互会话都要限时或在片内退出
- 安装/卸载/清数据类写操作先记录现状（包版本、数据状态），操作可回滚
- 提取的文件落盘 `mobile/` 并记录来源设备与路径，汇报引用相对路径
- 设备断开（offline/unauthorized）时先 `adb kill-server && adb start-server` 恢复，不要反复重试命令

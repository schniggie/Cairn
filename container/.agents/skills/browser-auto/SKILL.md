---
name: browser-auto
description: 需要真实浏览器渲染页面时使用：playwright-cli 驱动 chromium 做截图、DOM 快照、表单填写点击、登录态保存；遇到强 JS 渲染页面、需要登录交互的 Web 目标、或要给前端漏洞留证据截图时想到它
---

## 用法

* 镜像内全局装好 `playwright-cli`，浏览器为 chromium（PLAYWRIGHT_MCP_BROWSER=chromium），默认无头模式
* 工作流固定：open → snapshot 拿元素 ref → click/fill 操作 → screenshot/snapshot 留证据 → close
* 完整命令列表看 `playwright-cli --help`；会话在命令之间保持，close 后丢失

打开页面并截图（证据落盘 browser/）：

```bash
mkdir -p browser && playwright-cli open https://target.example && playwright-cli screenshot --filename=browser/home.png
```

DOM 快照拿元素引用（输出可访问性树，元素带 eN 编号）：

```bash
playwright-cli snapshot --filename=browser/snapshot.yml
```

登录交互（按 ref 填表单、点按钮，再保存登录态）：

```bash
playwright-cli fill e12 'admin' && playwright-cli fill e15 'Password123' && playwright-cli click e18
playwright-cli state-save browser/auth-state.json
```

带登录态复开并抓取登录后页面：

```bash
playwright-cli open https://target.example/dashboard && playwright-cli state-load browser/auth-state.json && playwright-cli reload
playwright-cli screenshot --filename=browser/dashboard.png
```

结束时清理浏览器进程：

```bash
playwright-cli close-all
```

## 规则

- 证据（截图、快照、登录态）一律 `--filename` 落盘到当前项目 workspace 的 `browser/` 下
- 元素操作优先用 snapshot 给的 ref（eN），页面变了就重新 snapshot，不要盲点坐标
- 时间片结束前必须 `playwright-cli close`/`close-all` 释放浏览器，不留孤儿 chromium 进程
- 不要把浏览器当扫描器用（遍历几百个 URL 截图）；批量 URL 先用 curl/katana 筛过，浏览器只看关键页面
- 登录态文件含敏感会话，放 `browser/` 下并在汇报中注明路径，不要粘进消息正文

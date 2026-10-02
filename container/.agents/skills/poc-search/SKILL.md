---
name: poc-search
description: 按 CVE 编号或组件名检索现成 PoC/EXP 时使用：镜像内置 CVE-PoC（近年 CVE 合集）、exphub（中文漏洞脚本集）、2023Hvv_（护网红队利用集）、Awesome-POC（索引型合集）、vulhub（漏洞环境与复现文档）；指纹确认组件版本后找利用代码时想到它
---

## 用法

* PoC 仓库在 `/home/kali/pocs/`，构建时快照，离线可用
* 检索优先级：先按 CVE 编号精确找，再按组件名模糊找；找到后先读代码再执行
* vulhub 的价值在 README 复现步骤与请求样例，不一定要起它的 docker 环境

按 CVE 编号全库定位：

```bash
fd -i "CVE-2024-21762" /home/kali/pocs
```

按组件名跨库检索（列文件）：

```bash
rg -il "confluence" /home/kali/pocs
```

找某组件在 vulhub 的复现文档目录：

```bash
fd -i "shiro" /home/kali/pocs/vulhub -t d
```

在护网/红队合集里按漏洞类型找脚本：

```bash
rg -il "fastjson|log4j" /home/kali/pocs/2023Hvv_ /home/kali/pocs/exphub
```

## 规则

- PoC 质量参差且可能带恶意代码：执行前必须通读源码，确认目标地址、payload 行为、有无回连外联，再决定运行
- 先把 PoC 复制到当前项目 workspace（如 `exploit/` 下）再按目标改写，不要在仓库目录里原地改
- 同一个漏洞一次只验证一个最匹配的 PoC，失败先分析原因再换，不要批量跑一堆脚本撞目标
- 第三方 PoC 的外联地址（DNSLog、反连 IP）一律替换为自己的监听，禁用不明外联
- PoC 执行的目标、参数、输出落盘记录；漏洞环境与影响面描述可引用仓库内 README 的相对路径

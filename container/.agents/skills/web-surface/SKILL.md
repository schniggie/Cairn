---
name: web-surface
description: 对 Web 目标做攻击面测绘时使用：dirsearch 目录枚举、nikto 服务侧配置检查、nuclei 模板化漏洞扫描；拿到 Web URL 后的标准起手式
---

## 用法

* nuclei 模板库已随镜像固定在 `/home/kali/.local/nuclei-templates` 并配置好自动加载，不要执行 `nuclei -update`
* dirsearch/nikto 输出默认进终端，务必显式落盘
* gobuster（kali 元包）也可用于目录/DNS 枚举，按需选用

目录枚举（过滤 403/404，常见扩展名）：

```bash
mkdir -p web && dirsearch -u https://target.example -x 403,404 --format plain -o web/dirsearch.txt
```

nikto 服务侧检查（SSL 目标直接用 https）：

```bash
nikto -h https://target.example -o web/nikto.txt
```

nuclei 按严重级别扫（先 critical+high，噪声小）：

```bash
nuclei -u https://target.example -severity critical,high -o web/nuclei-high.txt
```

nuclei 按标签扫（CVE 与信息泄露面）：

```bash
nuclei -u https://target.example -tags cve,exposure,config -o web/nuclei-cve.txt
```

## 规则

- 结果一律落盘到当前项目 workspace（如 `web/` 子目录），artifact 路径在汇报时引用相对路径
- 不要加大线程/速率参数；默认并发已足够，打挂目标等于毁掉自己的攻击面
- nuclei/dirsearch 的结果存在误报，汇报前先人工复核关键发现（curl 复现一次）
- 大目标（大量子域名/URL 清单）分批扫描，每批落盘后再扫下一批，不要一次性跑数小时
- 扫描字典/模板噪声大，先跑高置信集合（critical/high、cve 标签），再视情况扩大范围

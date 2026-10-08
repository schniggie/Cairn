---
name: recon-scan
description: 对目标主机或网段做侦察与端口/服务发现时使用，覆盖 naabu 快速端口发现、nmap 服务识别、katana Web 爬虫；拿到新目标（IP、域名、URL）后的第一批动作通常来自这里
---

## 用法

* 先用 naabu 做快速端口发现，再对存活端口用 nmap 做服务/版本精扫，不要一上来就 `nmap -p-`
* katana 用于从 Web 入口爬出 URL 清单，供后续 Web 测试使用
* kali-linux-headless 元包里还有 masscan、dnsenum、whois 等可按需使用

快速端口发现（top 1000 端口）：

```bash
mkdir -p recon && naabu -host 10.0.0.5 -top-ports 1000 -o recon/naabu.txt
```

对发现的端口做服务识别（-sC 跑默认脚本）：

```bash
nmap -sV -sC -p 22,80,443,445,8080 -oN recon/nmap-services.txt 10.0.0.5
```

全端口 TCP 扫（限速，适合后台分段时间片执行）：

```bash
nmap -p- --min-rate 1000 --max-retries 2 -oN recon/nmap-allports.txt 10.0.0.5
```

Web 爬虫（深度 3，输出 URL 清单）：

```bash
katana -u https://target.example -d 3 -silent -o recon/katana-urls.txt
```

## 规则

- 所有扫描结果必须用 `-o`/`-oN` 落盘到当前项目 workspace（如 `recon/` 子目录），不要只留在终端回显里
- 先小范围后大范围：先 top ports、单主机，确认存活与授权范围后再扩到全端口/网段
- 限速限并发，避免打满目标或触发防御；对生产目标默认不用 `--min-rate` 超过 5000
- 全端口/网段级扫描耗时不可控，拆成多个时间片分段执行，每段结果先落盘再继续
- 不要用 `nohup`/`setsid`/`&` 让扫描脱离当前执行片；时间片结束前主动终止并保留已落盘结果
- nmap 结果去噪：`tcpwrapped` 不算确认服务，只代表端口可达，标注待复核，不计入已确认成果
- 单主机扫出几百个开放端口大概率是负载均衡 VIP（对任意端口都应答），按一台设备算，不要按端口数夸大攻击面
- 全域 `tcpwrapped` 占比超过 50% 说明有强过滤/IPS 干扰，当前结果不可信，换扫描方式（降速、换探测类型、用 naabu 交叉验证）

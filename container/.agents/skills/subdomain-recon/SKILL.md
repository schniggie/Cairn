---
name: subdomain-recon
description: 做域名资产发现链路时使用：crt.sh 证书透明度与 dnsrecon 做子域名枚举、解析验证存活、再把存活域名交给 naabu/katana 做端口与 Web 测绘；目标是域名、需要摸清整个域的攻击面时想到它（与 recon-scan 分工：它管 IP/端口深度扫，本技能管域名资产发现）
---

## 用法

* 先被动后主动：crt.sh 不向目标发包先跑，dnsrecon 爆破是主动动作放后面
* 每阶段结果合并去重后再进下一阶段，全程落盘 `recon/`
* 存活主机的端口/服务深度扫交给 recon-scan 技能，Web 入口测试交给 web-surface/web-exploit

证书透明度查子域名（被动，零接触，%25 是通配符 % 的 URL 编码）：

```bash
mkdir -p recon && curl -s "https://crt.sh/?q=%25.example.com&output=json" | jq -r '.[].name_value' | sed 's/^\*\.//' | sort -u > recon/crtsh-subs.txt
```

dnsrecon 标准记录枚举（NS/MX/SRV 等，顺带尝试 AXFR 区域传送）：

```bash
dnsrecon -d example.com -t std -j recon/dnsrecon-std.json
```

dnsrecon 字典爆破子域名（主动、有噪声，字典自备）：

```bash
dnsrecon -d example.com -t brt -D <子域名字典> -j recon/dnsrecon-brt.json
```

合并去重并验证存活（能解析出 A 记录才算存活）：

```bash
jq -r '.[].name' recon/dnsrecon-std.json recon/dnsrecon-brt.json 2>/dev/null | cat - recon/crtsh-subs.txt | sort -u > recon/all-subs.txt
while read -r d; do dig +short "$d" A | grep -q . && echo "$d"; done < recon/all-subs.txt | tee recon/alive-subs.txt
```

存活清单衔接端口发现与 Web 爬虫：

```bash
naabu -l recon/alive-subs.txt -top-ports 1000 -o recon/naabu-subs.txt
katana -list recon/alive-subs.txt -d 2 -silent -o recon/katana-subs.txt
```

## 规则

- 每一步确认有必要再升级：crt.sh 够用就不爆字典，爆字典前确认授权范围
- 泛解析（wildcard）会让所有子域名"存活"：先用随机子域探测，存在泛解析就按解析 IP/响应特征过滤
- 字典爆破限字典规模，属高噪声动作，不要对大域无差别跑
- 清单类结果一律 `sort -u` 去重后落盘 `recon/`，汇报引用相对路径
- 大清单分批验证存活，每批先落盘再继续下一批

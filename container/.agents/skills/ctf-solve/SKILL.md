---
name: ctf-solve
description: CTF 比赛拿到题目后不确定类型时的解题编排层：用通用侦察命令判断题目类别，按路由表分派到专项技能（binary-rev、web-exploit、cred-secrets 等），并提供 flag 没找到时的诊断清单；CTF/比赛场景拿到附件或目标入口时先看它
---

## 用法

* 本技能只做分类与分派：侦察 → 定类 → 读对应技能的 SKILL.md 再深入，不要在本技能里直接开打
* tsec 比赛拿到 flag 后的提交动作见 tsec-actions 技能

通用侦察（对题目附件先跑一遍，binwalk/exiftool 为 kali 自带）：

```bash
mkdir -p ctf && file <附件> && checksec --file=<附件> 2>/dev/null | tee ctf/recon.txt
strings -n 6 <附件> | head -50
binwalk <附件>
exiftool <附件>
```

按文件类型路由：

| 附件/入口 | 分类 | 去哪个技能 |
|---|---|---|
| Web URL / HTML / JS / PHP | Web | web-exploit（先 web-surface 测绘） |
| ELF/PE 需理解逻辑 | Reverse | binary-rev |
| ELF + 远程服务端口 | Pwn | binary-rev 读逻辑 → exploit-toolkit 写 exp |
| 哈希 / JWT / 密钥材料 | 凭据 | cred-secrets |
| .pcap/.pcapng | 流量分析 | 无专项技能：strings/十六进制提取，发现凭据接 cred-secrets |
| 图片 / 音频 / PDF | 隐写 | 无专项技能：exiftool 元数据、binwalk 分离、strings 扫尾 |
| 压缩包 / 一串密文 | Crypto | 无专项技能：先识别编码层，哈希接 cred-secrets 破解 |
| 目标 IP/网段 | 渗透 | recon-scan 起手 |

按题目关键词路由：overflow/ROP/shellcode→Pwn；RSA/AES/cipher→Crypto；XSS/SQLi/JWT/SSRF→Web；disk image/memory dump→取证。

flag 快搜（任何阶段都可以试，前缀按题目要求调整）：

```bash
grep -rniE 'flag\{|ctf\{' .; strings <附件> | grep -iE '\{.*\}'
```

flag 没找到时的诊断清单：

1. 回题目描述找暗示：题目名与描述里的双关词往往直接指明手法（"listen"→流量，"layers"→隐写/分层）
2. 换编码：base64/hex/rot13/URL 编码套娃很常见，逐层解码再搜
3. 换工具：同一文件用不同工具看，binwalk 没结果就 exiftool/strings/十六进制看文件头尾
4. 重新分类：很多题跨类（Web+Crypto、Reverse+Pwn），按另一类重新走一遍
5. 检查遗漏：隐藏文件、HTTP 响应头、robots.txt、源码注释、备份文件（.bak/.swp/~）
6. 已利用但没 flag：按漏洞类型想位置——SQLi 在数据库表里、LFI 读 /flag*、RCE 后全盘 find/grep

## 规则

- 先侦察再分类，不要凭题目名假设类型；分类错了按诊断清单第 4 条换类
- 深入某类时必须先读对应技能的 SKILL.md，本技能只负责指路
- 一个思路卡住超过两轮就按诊断清单换方向，不要硬撞
- 中间产物（提取的文件、解码结果、笔记）落盘 `ctf/` 子目录，flag 证据一并落盘
- 拿到 flag 后核对格式（`flag{...}` 或题目指定前缀），高置信再提交

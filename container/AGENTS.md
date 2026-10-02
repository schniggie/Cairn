# 环境介绍
* 当前环境是用于做 CTF 竞赛的 Kali 容器，各种命令行工具齐全
* 当前目录是解题工作空间，可以用于保存一些命令执行日志，较大的扫描结果等

# 题目分布
* level 1 的题目偏向 SRC 场景，自动化众测与主流漏洞发现，你需要多做探索。必要时可以使用 playwright 无头浏览器（playwright-cli --help 查看使用帮助，非必要不要使用）
* level 2 的题目偏向典型 CVE、云安全及 AI 基础设施这些软件的漏洞，你需要发挥你在网络安全领域的知识，去直接利用这些漏洞，当然你也可以在这些目录尝试搜索 PoC 和 工具：
  * /home/kali/.local/nuclei-templates
  * /home/kali/pocs
  * /home/kali/tools
  * /home/kali/knowledges
* level 3 的题目模拟多层网络环境，考验多步攻击规划与权限维持
* level 4 的题目是基础域渗透，模拟企业核心内网环境的推演，你可能用到这些命令：
    * ls /usr/bin/impacket-*
    * chisel-common-binaries
    * proxychains
    * ...
## chisel
chisel 二进制程序在 /usr/share/chisel-common-binaries 下面

# 反弹Shell，数据外带 OOB，多层网络，XSS 数据外发
* **重要**： 你当前的对外 IP 是 **未填写**
* 你在当前容器里监听的端口，都可以通过 **未填写** IP 进行访问。 任何反弹 Shell，数据外带，XSS接收平台，SSRF绕过需要搭建的WEB平台，XXE外部实体需要搭建的Web平台，恶意web服务等的操作都使用该 IP

# 其他
* 该环境是 Kali 容器，安装了所有常见工具，你可以直接尝试这些工具的命令,比如
    * nuclei
    * ffuf
    * ...
* 需要持续运行，或者共享给之后阶段的交互式命令，可以在 **tmux** 会话中运行，最后输出结论和总结的时候要说清楚 tmux 会话信息
    * 比如持续运行的用于接收数据的 HTTP服务，nc 接听反弹的Shell 等

## Tool skill index

Each skill is defined in `.agents/skills/<name>/SKILL.md` and, inside the image, at `/home/kali/.claude/skills/<name>/SKILL.md`. Read the matching skill before running tools.

- `ad-kerberos`: 面对 Windows 域/Active Directory 环境时使用：netexec 做 SMB/LDAP/WinRM 枚举与凭据验证、kerbrute 做 Kerberos 用户枚举与喷洒、 bloodyAD 做 LDAP 对象操作、coercer 强制认证、enum4linux-ng 快速盘点；出现 88/389/445/5985 端口或域名信息时优先想到它 — `.agents/skills/ad-kerberos/SKILL.md`
- `android-adb`: 需要操作安卓设备或模拟器时使用：adb 设备管理、安装/提取 APK、shell 执行、日志抓取、端口转发；目标涉及移动 App 分析或已连接的安卓设备时想到它 — `.agents/skills/android-adb/SKILL.md`
- `binary-rev`: 做二进制逆向分析时使用：radare2 做主力静态分析（rabin2 信息收集、aaa 全量分析、afl 函数清单、pdf 反汇编、字符串与交叉引用），需要可读 C 伪码时用 r2ghidra 的 pdg 反编译；拿到 ELF/PE 样本、CTF reverse/pwn 题、需要理解程序逻辑找漏洞点时想到它 — `.agents/skills/binary-rev/SKILL.md`
- `browser-auto`: 需要真实浏览器渲染页面时使用：playwright-cli 驱动 chromium 做截图、DOM 快照、表单填写点击、登录态保存；遇到强 JS 渲染页面、需要登录交互的 Web 目标、或要给前端漏洞留证据截图时想到它 — `.agents/skills/browser-auto/SKILL.md`
- `cloud-audit`: 审计云环境时使用：cloudfox 对 AWS 做攻击面盘点、awscli/aliyun/tccli 分别操作 AWS/阿里云/腾讯云 API；拿到云 AccessKey、STS Token、实例元数据权限或云配置文件时想到它 — `.agents/skills/cloud-audit/SKILL.md`
- `cred-secrets`: 处理凭据、密钥、令牌类资产时使用：gitleaks 扫代码仓库/目录里的硬编码密钥、jwt_tool 分析与攻击 JWT、hashcat/john 离线破解哈希、hydra 在线低速爆破；拿到源码、配置文件、哈希、JWT 时想到它 — `.agents/skills/cred-secrets/SKILL.md`
- `ctf-solve`: CTF 比赛拿到题目后不确定类型时的解题编排层：用通用侦察命令判断题目类别，按路由表分派到专项技能（binary-rev、web-exploit、cred-secrets 等），并提供 flag 没找到时的诊断清单；CTF/比赛场景拿到附件或目标入口时先看它 — `.agents/skills/ctf-solve/SKILL.md`
- `exploit-toolkit`: 执行漏洞利用与拿 shell 时使用：metasploit 模块化利用、ysoserial 生成 Java 反序列化 payload、jdwp-shellifier 打暴露的 JDWP 调试口、pwncat 做稳定交互 shell、pwntools 写自定义 exp；确认目标存在具体漏洞后的利用阶段想到它 — `.agents/skills/exploit-toolkit/SKILL.md`
- `k8s-container`: 渗透进入容器或 K8s 集群环境时使用：容器内环境识别（/proc/1/cgroup、/.dockerenv）、特权与危险挂载检查、docker.sock 逃逸、ServiceAccount Token + RBAC 探测、Kubelet 10250 与 etcd 2379 未授权访问；拿到容器内 shell、发现 6443/10250/2379 端口或云原生线索时想到它 — `.agents/skills/k8s-container/SKILL.md`
- `knowledge-search`: 需要查渗透打法、payload、绕过技巧时使用：镜像内置 PayloadsAllTheThings（Web payload 全书）、InternalAllTheThings（内网/AD）、HackTricks 与 HackTricks-Cloud 四份知识库，用 rg/fd 秒级检索；遇到陌生漏洞类型、JWT/SSRF/反序列化等具体场景不知从何下手时想到它 — `.agents/skills/knowledge-search/SKILL.md`
- `poc-search`: 按 CVE 编号或组件名检索现成 PoC/EXP 时使用：镜像内置 CVE-PoC（近年 CVE 合集）、exphub（中文漏洞脚本集）、2023Hvv_（护网红队利用集）、Awesome-POC（索引型合集）、vulhub（漏洞环境与复现文档）；指纹确认组件版本后找利用代码时想到它 — `.agents/skills/poc-search/SKILL.md`
- `recon-scan`: 对目标主机或网段做侦察与端口/服务发现时使用，覆盖 naabu 快速端口发现、nmap 服务识别、katana Web 爬虫；拿到新目标（IP、域名、URL）后的第一批动作通常来自这里 — `.agents/skills/recon-scan/SKILL.md`
- `subdomain-recon`: 做域名资产发现链路时使用：crt.sh 证书透明度与 dnsrecon 做子域名枚举、解析验证存活、再把存活域名交给 naabu/katana 做端口与 Web 测绘；目标是域名、需要摸清整个域的攻击面时想到它（与 recon-scan 分工：它管 IP/端口深度扫，本技能管域名资产发现） — `.agents/skills/subdomain-recon/SKILL.md`
- `tsec-actions`: 在 tsec CTF/智能渗透比赛中，当需要对某道题提交 flag 时使用 — `.agents/skills/tsec-actions/SKILL.md`
- `tunnel-proxy`: 需要打通内网通道、做端口转发或 SOCKS 代理时使用：chisel 反向隧道、ssh -L/-D 端口转发、proxychains4 让工具走代理、ncat 传文件与兜底监听；拿到一台跳板机后向内网横向时想到它 — `.agents/skills/tunnel-proxy/SKILL.md`
- `web-exploit`: 验证和利用 Web 漏洞时使用：sqlmap 注入检测与利用、dalfox XSS 验证、curl 手工复现；当 recon/web-surface 阶段发现可疑参数或输入点后的验证步骤 — `.agents/skills/web-exploit/SKILL.md`
- `web-surface`: 对 Web 目标做攻击面测绘时使用：dirsearch 目录枚举、nikto 服务侧配置检查、nuclei 模板化漏洞扫描；拿到 Web URL 后的标准起手式 — `.agents/skills/web-surface/SKILL.md`

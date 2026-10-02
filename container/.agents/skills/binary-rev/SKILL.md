---
name: binary-rev
description: 做二进制逆向分析时使用：radare2 做主力静态分析（rabin2 信息收集、aaa 全量分析、afl 函数清单、pdf 反汇编、字符串与交叉引用），需要可读 C 伪码时用 r2ghidra 的 pdg 反编译；拿到 ELF/PE 样本、CTF reverse/pwn 题、需要理解程序逻辑找漏洞点时想到它
---

## 用法

* 不使用 IDA；反编译优先 `pdg`（r2ghidra），伪码读不懂或插件失败再退回 `pdf` 精读反汇编
* r2 一律 `-q -c` 非交互批量执行，命令结果直接落盘；交互界面（`V`）只作临时浏览
* 最小工作流：file/checksec → rabin2 → aaa → afl/字符串 → 定位关键函数 → pdf/pdg
* 定位到漏洞点后写利用脚本，衔接 exploit-toolkit 技能（pwntools）

起手：识别文件类型与保护机制（checksec 由 pwntools 提供）：

```bash
mkdir -p rev && file ./sample && checksec --file=./sample | tee rev/checksec.txt
```

rabin2 信息收集（-I 基本信息含 pie/relro/nx，-z 数据段字符串，-i 导入表）：

```bash
rabin2 -I ./sample | tee rev/info.txt
rabin2 -z ./sample | tee rev/strings.txt
```

全量分析（-A 等价于先跑 aaa）并列出函数清单：

```bash
r2 -q -A -c 'afl' ./sample | tee rev/functions.txt
```

看关键函数的反汇编与 r2ghidra 伪码：

```bash
r2 -q -A -c 's sym.main; pdf' ./sample | tee rev/main.asm.txt
r2 -q -A -c 's sym.vuln; pdg' ./sample > rev/vuln.c
```

从可疑字符串反推逻辑：先搜字符串，再用 axt 查谁引用它：

```bash
r2 -q -A -c 'iz~password' ./sample
r2 -q -A -c 'axt @ 0x00401234' ./sample
```

r2 常用命令速查：

| 命令 | 作用 |
|---|---|
| `aaa` | 全量自动分析（函数、符号、xref） |
| `afl` | 列出所有函数 |
| `iz` / `izz` | 数据段字符串 / 全文件字符串 |
| `axt @ 地址` | 查谁引用了该地址/函数 |
| `s 符号` | 跳转到符号或地址 |
| `pdf` | 反汇编当前函数 |
| `pdg` | r2ghidra 反编译当前函数为 C 伪码 |
| `/ 字符串` | 全文件搜索 |
| `命令?` | 任一命令加 `?` 看帮助（如 `pd?`） |
| `q` | 退出 |

## 规则

- 样本一律视为不可信：只做静态分析，不要直接在容器里运行未知二进制
- 分析产物（信息、函数清单、反汇编、伪码）落盘 `rev/` 子目录，结论与漏洞点假设写 `rev/notes.md`，汇报引用相对路径
- stripped 二进制没有符号名：从入口或字符串 xref 反推关键函数，在 afl 里找调用关系定位 main
- `pdg` 对大函数可能慢或失败：先用 `s` 缩到单个函数再反编译，失败后退回 `pdf` 精读
- 每条 r2 命令输出直接重定向落盘，不要在消息正文里贴大段反汇编

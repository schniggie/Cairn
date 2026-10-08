---
name: knowledge-search
description: 需要查渗透打法、payload、绕过技巧时使用：镜像内置 PayloadsAllTheThings（Web payload 全书）、InternalAllTheThings（内网/AD）、HackTricks 与 HackTricks-Cloud 四份知识库，用 rg/fd 秒级检索；遇到陌生漏洞类型、JWT/SSRF/反序列化等具体场景不知从何下手时想到它
---

## 用法

* 知识库在 `/home/kali/knowledges/`，是构建时的 git 快照，离线可用
* 先用 `rg -l` 只列文件名定位章节，再对单文件精读关键段落，避免整库内容灌进上下文
* 按库分工：Web 查 PayloadsAllTheThings，内网/AD 查 InternalAllTheThings，通用方法论查 hacktricks，云查 hacktricks-cloud

按关键词定位相关文档（只列文件）：

```bash
rg -il "saml|xxe" /home/kali/knowledges/PayloadsAllTheThings
```

按文件名找某主题的 payload 文件：

```bash
fd -i "jwt|graphql" /home/kali/knowledges/PayloadsAllTheThings
```

内网主题检索（带上下文看用法）：

```bash
rg -i -C 3 "kerberoast|dcsync" /home/kali/knowledges/InternalAllTheThings
```

云渗透打法检索：

```bash
rg -il "metadata|instance profile" /home/kali/knowledges/hacktricks-cloud
```

## 规则

- 知识库内容只读，不要在其中做任何修改；提炼出的可用 payload/命令复制到工作目录再按目标改写
- 检索词用英文（库都是英文文档），多组同义词用 `|` 一次搜全
- 每次只精读最相关的一两个文件；`rg` 加 `-C` 控制上下文行数，避免一次输出过长
- 知识库是快照可能滞后，具体 CVE 细节配合 poc-search 技能找现成 PoC 交叉验证
- 从知识库学到的打法要在汇报里落为对当前目标的实际动作，不要只摘抄文档

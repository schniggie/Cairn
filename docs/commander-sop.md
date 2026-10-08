# 总指挥实战 SOP(测试赛实战总结)

本文件记录在西湖论剑测试赛实战中踩过的坑和验证过的流程。新会话接手当总指挥前,先读这份,再读根目录 CLAUDE.md。

## 0. 一句话定位

**你的任务不是解题,是安排 Agent 解题。** 解不出或解错,靠干预让 Agent 重解,不是自己下场。

## 0.5 干预原则(重要)

**非必要不加 hint。** 默认让 Agent 自主探索解题,不加任何方向引导。只有符合以下情况才介入:
- 同一题**卡了很久很久**(远超过 reason/explore 的正常轮数,如几十分钟无实质进展)
- 提交 flag 失败且确认不是格式问题(如带壳)
- 出现死循环/反复失败(看 `retry_count` 持续上涨)

即使介入,也从轻到重:先加 hint 引导 → 再考虑 reopen → 最后才换模型/策略。不要一建题就塞 hint 替 Agent 指方向(实测:带 hint 和不带 hint 解出的结果一致,但会限制 Agent 的探索思路)。

## 1. 核心教训:Agent 会轻信表面答案

实测:Agent 解出一道"解压缩"题,解出 `DASCTF{ni_cai?}` 就 **complete 了项目**,但这是假 flag("ni_cai" = "你猜?" 的谐音),提交被平台判错。

**含义**:Agent 找到第一个像 flag 的东西就会宣布成功。总指挥必须核实,不能看到 project completed 就信。

**核实动作**:
1. `GET /projects/{id}/export?format=yaml` 看 facts,确认 agent 到底解出了什么
2. 如果 flag 可疑(谐音、占位、和题目描述雷同),别急着提交

## 2. 干预流程(Agent 解错 / 卡住 / 死循环)

正确动作是 **reopen + 加 hint**,不是删了重建(重建会丢已探索的 facts,浪费)。

```bash
# 1. reopen 回到 active(会加一个 external_feedback fact)
curl -X POST http://127.0.0.1:8000/projects/<id>/reopen \
  -H 'Content-Type: application/json' \
  -d '{"description": "...", "creator": "commander"}'   # 避免中文,用文件方式传 JSON

# 2. 加 hint 指明方向(核心:告诉 Agent 为什么错、往哪找)
curl -X POST http://127.0.0.1:8000/projects/<id>/hints \
  -H 'Content-Type: application/json' --data-binary @/tmp/hint.json
```

**hint 要具体**。实测有效示例:
> "提交 X 被平台判定错误。X 是假 flag。真正的 flag 藏在压缩包结构里——检查嵌套 zip 的文件名序列、zip 注释、隐藏文件、文件大小/时间戳等元数据。提取后提交验证。"

reopen + hint 后,dispatcher 会自动重新派 reason/explore,Agent 会接着新线索解。

**干预后要观察**:`docker logs cairn-dispatcher --tail` 看它是否真的重新派活(应该有 `dispatched reason ... trigger=facts:x->y`)。

## 3. 提交 flag 的判定(别浪费提交次数)

⚠️ **最关键的一条:平台只认 flag 内容,不认 `DASCTF{}` 外壳!**

```json
{"exerciseId": 10661, "flag": "38764470093573754137979128205989"}   ✅ 成功
{"exerciseId": 10661, "flag": "DASCTF{38764470093573754137979128205989}"}  ❌ flag错误
```

**提取到 `DASCTF{...}` 后,提交前必须剥掉外壳只提交内容。** `submit_flag.py` 和 `watchdog.py` 已自动剥壳。这条是本次测试赛踩的最大坑:agent 和系统都解对了 flag,只因带壳提交全被拒,白白浪费多次提交机会。

平台 `POST /answer-panel/answer` 响应:
- `code=00000` + `data.isCorrect=true` → ✅ 成功
- `code=40001` message="提交flag错误" → ❌ flag 内容不对,**先确认是否带壳了**,再考虑 Agent 是否解错
- 鉴权错误 → accesskey 问题
- 注意有提交次数限制(实测剩余次数递减)

**不要连续提交猜的 flag**。提交一次失败,就该走干预流程让 Agent 重解,而不是自己试。

## 4. 建题必须给足信息

Agent 在容器里解题,它只能靠 project 的 origin + hints 知道要干什么。所以建题时:

- **origin(题目描述)必须包含**:
  - 题目原文描述
  - **附件下载 URL**(如果有附件,Worker 要自己去下载)
  - **flag 格式提示**(如 "flag 格式为 DASCTF{}")
  - 环境题的**靶机连接信息**(见下)
- **hint**:按题型给方向(如解压题:"注意 binwalk 检查隐藏内容")

**附件题 vs 环境题**:
- 附件题(`isNeedInit=false`,如"解压缩"):origin 带附件 URL 即可
- 环境题(`isNeedInit=true`,如 Web/Pwn):**先 `build-exercise-env` 启动环境,轮询详情直到 `endpoints` 非空**,拿到靶机 IP/端口/账号,把连接信息塞进 origin 或 hint,Agent 才知道连哪

## 5. 平台限制(测试赛实测)

| 限制 | 值 | 应对 |
|---|---|---|
| 限流 | 429(详情请求太快触发) | 请求间隔 + 退避重试(fetch 脚本已处理) |
| 同时靶机数 | **单账号最多 3 台** | 批量建环境题要控制并发,超过要等回收 |
| flag 格式 | `DASCTF{...}` | 正则已兼容 DASCTF/flag |
| 单 Agent 接入 | 每队只允许一个 Agent | racing 只能在 Cairn 内部做,不能开多账号 |
| 禁 flag 爆破 | - | Agent 不能穷举试 flag |
| 解题报告 | 结束前在线提交 | 赛后审核报告+网络流量+平台日志 |

## 6. 网关(LLM 调用必须走)

- Agent 的 `ANTHROPIC_BASE_URL` = `https://llm-gateway.dasctf.com/llm-gateway/proxy/e/<token>`
- API key 由 Agent 自己持有,网关只转发 + 记录
- **不走网关 = 成绩失效**。验证方式:dispatch.yaml 配好后看 dispatcher 健康检查 `HTTP 200`

## 7. 编码坑(Windows 开发时)

- **Windows shell 传中文 JSON 会坏**:`curl -d '{"x":"中文"}'` 会被 GBK 转码弄坏 → 用 `--data-binary @file` 传 UTF-8 文件
- **终端显示乱码 ≠ 数据乱码**:平台返回是正常 UTF-8,是 Windows 控制台 GBK 显示问题。用 Python `sys.stdout.reconfigure(encoding='utf-8')` 或写文件看
- **extract_flag 要排除占位文字**:题目描述里"flag格式为DASCTF{}"这种会匹配到,别当真 flag。优先看 facts 里的真实内容

## 8. 端到端验证过的完整链路(可复现)

```
fetch_challenges.py → 拉题目列表+详情(含难度/附件/是否需init)
create_projects.py → 批量建 project(origin 带附件URL/靶机信息 + hint)
dispatcher → 创建 Kali 容器 → claude CLI 调网关 → DeepSeek 解题
→ 出 flag → extract_flag.py 提取 → submit_flag.py 提交 → 平台判定
```

全套脚本在 `scripts/`。配置在 `scripts/competition.yaml`。

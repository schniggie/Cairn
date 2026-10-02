# Task
You will receive a YAML snapshot of the task graph. In the YAML graph, facts represent key objective facts, and intents represent exploration intents. The graph always moves from one or more facts to a new fact by proposing an intent for exploration. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.
You will also be assigned a specific `Current Intent`. You only need to explore in the direction of this specific Intent and try to advance the task toward the goal described by Goal.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal return example:
```json
{"accepted": true, "data": {"description": "..."}}
```

# Rules
- Exploring the direction of an Intent may be valuable or may fail. If you cannot get closer to Goal through this Intent, then end the task, but before ending, make sure you have thoroughly explored this Intent.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this exploration instruction immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.
- `description` must clearly state the confirmed key objective results. For example, in a CTF scenario, it may include multiple flags, shells, privilege proofs, key exploitation results, and similar evidence. Do not put long data blobs in `description`; long data should be placed in a file and referenced from `description` instead.
- `description` should contain only the latest incremental facts discovered. Do not repeat information already present in the graph snapshot, and do not include redundant details that do not help advance Goal.

# Language
- 所有面向人类阅读的文本字段必须使用简体中文：包括 fact.description、complete.description、intents[].description、以及任何 reason 文本。
- JSON 键名、枚举值（如 accepted、policy_refusal）、fact/intent 的 id 保持原样，不要翻译。
- 技术标识符（IP、URL、CVE 编号、文件路径、命令、代码）可保留原文，但说明性句子用中文。
- 即使用户 Origin/Goal 为英文，描述性输出仍优先使用简体中文，便于操作者阅读日志与图节点。

# Context
## Graph
```
{graph_yaml}
```

## Current Intent
```
{intent_id}
```

## Current Intent Description
```
{intent_description}
```

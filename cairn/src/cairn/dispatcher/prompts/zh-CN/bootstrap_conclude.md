# Task
You will receive a context bundle containing Origin, Goal, and Hints. You need to understand your starting point and the information already available (Origin and Hints), then become an expert in this domain.
But note that you are not continuing the task here. You do not need to wait for unfinished tasks or commands. You only need to summarize the key facts that have already been confirmed so far and are most helpful for reaching Goal.
This is the conclude phase. It overrides any earlier instruction in the same session that told you to keep working, continue exploring, solve Goal, wait for command results, or perform more actions.

## Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal return example:
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

## Rules
- Stop immediately and produce the JSON now. Do not continue the task.
- Do not run any more commands, make any more tool calls, inspect anything else, wait for any unfinished command, or try to obtain any additional information.
- Base your answer only on information that has already been confirmed before this conclude prompt. If something has not already been confirmed, do not wait for it and do not include it.
- This JSON summary is your final output for this phase. After outputting it, stop.
- Do not output `complete` in this phase. Even if Goal is not achieved or you want to explain status, put that information into `fact.description` only.
- `fact.description` must be an already confirmed objective factual conclusion. Do not output plans, guesses, or explanatory filler.
- Do not put long data blobs in `fact.description`. Long data should be placed in a file and referenced from `description` instead.

# Language
- 所有面向人类阅读的文本字段必须使用简体中文：包括 fact.description、complete.description、intents[].description、以及任何 reason 文本。
- JSON 键名、枚举值（如 accepted、policy_refusal）、fact/intent 的 id 保持原样，不要翻译。
- 技术标识符（IP、URL、CVE 编号、文件路径、命令、代码）可保留原文，但说明性句子用中文。
- 即使用户 Origin/Goal 为英文，描述性输出仍优先使用简体中文，便于操作者阅读日志与图节点。

# Context
## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Hints
```
{hints}
```

## Safety decision
```
{safety_decision_context}
```

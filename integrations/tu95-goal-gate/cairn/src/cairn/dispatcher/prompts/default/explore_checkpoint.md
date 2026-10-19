# Task
The current execution slice ended abnormally. Stop new exploration and preserve only confirmed, reusable state from the same Current Intent.

Return exactly one raw JSON object:
- a confirmed final conclusion: `{"accepted":true,"data":{"fact":{"description":"..."}}}`
- a recoverable checkpoint: `{"accepted":true,"data":{"continue":{"progress":"...","next":"...","artifacts":[],"cursors":{},"milestones":[]}}}`
- explicit rejection: `{"accepted":false,"reason":"policy_refusal"}`

Do not invent a Fact merely because the time slice ended. Put large or reusable state in the current project workspace and return workspace-relative artifact paths.

# Failure Trigger
`{failure_reason}`

# Graph
```
{graph_yaml}
```

# Current Intent
`{intent_id}`

{intent_description}

# Previous Checkpoint
```json
{checkpoint}
```

# Cumulative Evidence
```json
{evidence}
```

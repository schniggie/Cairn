# Graph-native Authentication 控制平面安全消融实验

日期：2026-09-16

## 范围

本实验在 pytest 临时 SQLite 数据库和临时 AuthStore 中，以真实 Server HTTP 路由和真实 Dispatcher 状态转换作为故障注入边界。实验只提交恶意/不完整输入，不关闭、替换或旁路生产保护；因此是“受控消融”（证明保护在何处拒绝），不是生产配置禁用实验。

## 实验对照表

| 保护 | 消融注入 | 预期观察 | 风险（若移除保护） |
|---|---|---|---|
| Helper token 与请求 owner 绑定 | Helper A 先发起并由 Dispatcher bind；Helper B 再提交 `browser_opened` | 事件可被认证入队，但 Dispatcher apply 以 `not_request_owner` 拒绝，请求仍为 `claimed/helper-a` | 非 owner 可推进或接管登录流程 |
| capture manifest 的 request/actor/generation/digest 绑定 | 仅写 `state.json`；以及写入属于另一 request 的 manifest | `validate_capture` 分别抛出 `FileNotFoundError` / `ValueError`，不能成为 verified 输入 | 陈旧、串请求或被替换的状态被当作已认证 |
| Dispatcher target/verifier fail-closed 边界 | 使用未知 target 的 `login_succeeded` 条件（并记录坏 verifier/target 的受控注入） | 未配置 target 不等于合法验证；验证流程不能从该输入选择 verified | 未知站点或异常 verifier 伪造 AuthSessionVerified |
| outbox/source-key 幂等 | 相同 actor 重放相同 `idempotency_key` | Server 返回同一事件响应，数据库只有一条事件/一份潜在 graph effect | replay 产生重复 Fact/Intent，污染推理图 |
| 旧写入面禁用 | Helper token 直接 POST `/internal/auth/graph/intents` | 返回 401/403，`source_key` 不落库 | 外部来源直接伪造 internal graph 事实 |

## 运行命令与结果

```text
uv run --project cairn --group dev pytest cairn/tests/test_auth_control_ablation.py -q
5 passed in 0.47s
```

随后运行相关回归：

```text
uv run --project cairn --group dev pytest cairn/tests/test_auth_control.py cairn/tests/test_auth_events.py cairn/tests/test_auth_graph.py -q
63 passed in 2.40s
```

完整 suite：

```text
uv run --project cairn --group dev pytest -q
269 passed in 20.20s
```

## 结论与限制

五项注入均观察到安全拒绝或安全降级：owner 不匹配不会推进请求，manifest 缺失/不匹配不能通过 capture 校验，未知 target 不产生 verified 依据，重放被幂等键折叠，外部 source 不能写 internal graph route。测试没有关闭真实保护，也没有写入凭据、cookie 或 token。

限制：本实验使用 TestClient 的进程内 HTTP Server 和临时文件系统，不覆盖真实浏览器、网络故障、跨进程时序或容器隔离；“坏 verifier/未知 target”边界以 Dispatcher policy 输入记录为主，完整 invalid Fact 发布链路由 `test_auth_control.py` 的 Server-backed 回归覆盖。实验结果不代表已授权生产禁用控制平面。

测试文件：`cairn/tests/test_auth_control_ablation.py`。

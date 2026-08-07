# Edge–Server 协议

状态：协议 v1 实现中。session/generation/sequence、安全控制面和 fake 双进程传输已实现；真实 backend 尚未接入。

## 1. 设计目标

协议必须保证：

- 无完整身份字段的请求无法进入 policy；
- 旧 Skill、旧 generation 或旧 session 请求无法获得 action；
- 同一时刻只有一个动作客户端 lease；
- policy inference 与 PRNG 更新严格串行；
- 连接中断时不会自动重复执行含糊请求；
- 控制面不暴露给局域网；
- 客户端与服务端都能生成相互关联的审计证据。

## 2. 传输层

### 2.1 控制面

Surf 使用 Unix domain socket：

```text
${OVEN_RUNTIME_ROOT}/control/oven-server.sock
```

权限必须为 `0600`，owner 为运行 server 的用户。Agilex 不直接连接 socket，而是执行受限 SSH 命令：

```text
ssh surf-target oven-serverctl --json <command> ...
```

`oven-serverctl` 只接受参数数组，不接受任意 Shell 字符串。SSH 账号后续可通过 `authorized_keys`
forced-command 限制为该控制程序。

### 2.2 推理面

Surf WebSocket 只绑定：

```text
127.0.0.1:19110
```

Agilex 建立本地转发：

```text
127.0.0.1:19110 → SSH → Surf 127.0.0.1:19110
```

服务启动后必须自检实际监听地址；如果监听到非 loopback 地址，启动失败。

### 2.3 编码与限制

- 控制面：一条 JSON request/response，由 Unix socket framing 管理。
- 推理面：WebSocket binary frame + 项目自有 MessagePack NumPy ExtType codec，不依赖 OpenPI 或旧项目 codec。
- text frame 一律拒绝。
- 默认最大请求 64 MiB，配置只能降低，不能在真机 profile 中设为 unlimited。
- 所有 schema 都携带 `protocol_version`。

## 3. 身份概念

### 3.1 Generation

每次成功激活新 policy 后递增的正整数。Generation 在以下情况下改变：

- Skill 改变；
- checkpoint/adapter 版本改变；
- server 明确重新激活当前 Skill；
- server 重启并重新加载 policy。

Generation 改变前必须撤销当前 session。

### 3.2 Session

Session 是唯一动作客户端租约，包含：

```json
{
  "session_id": "UUID",
  "session_token": "256-bit random secret",
  "skill": "open_door",
  "generation": 4,
  "expires_at": "RFC3339 timestamp"
}
```

`session_token` 只通过 SSH 控制面返回，不能写入日志、manifest 或异常文本。审计只记录 `session_id`。

### 3.3 Trial

Trial 是一次具备独立 seed 和证据目录的 rollout 尝试：

```text
run_id / stage_index / skill / attempt
```

Trial ID 必须满足固定字符集和长度限制，服务端使用 `mkdir(exist_ok=False)` 防止覆盖。

## 4. 控制命令

所有响应都包含：

```json
{
  "protocol_version": 1,
  "ok": true,
  "command": "status",
  "server_instance_id": "UUID",
  "result": {}
}
```

错误响应包含稳定 `error_code`，不能要求调用方解析自然语言。

### 4.1 `status`

只读返回：

- server instance ID；
- model state；
- active skill；
- generation；
- backend 和模型资产摘要；
- active session ID（不含 token）；
- in-flight request 数；
- active trial ID；
-最近一次错误代码。

### 4.2 `prepare-skill`

输入：

```json
{
  "skill": "open_door",
  "backend": "composite_adapter",
  "force_reload": false
}
```

行为：

1. 撤销 active session；
2. 进入 DRAINING；
3. 等待 in-flight inference 归零；
4. 加载并验证 policy；
5. generation 递增；
6. 进入 WARMUP_REQUIRED；
7. 返回加载证据。

加载失败进入 FAILED，不自动退回旧 policy，也不隐式启动其他 server。

### 4.3 `begin-trial`

输入 trial ID 和 root seed。只允许在 `WARMUP_REQUIRED` 且没有 active trial 时执行。创建审计目录，
但不将 server 置为 READY。

### 4.4 `open-session`

只允许：

- active trial 存在；
- model state 为 WARMUP_REQUIRED 或 READY；
- 没有 active session。

返回 session ID 和 token。第二个请求返回 `SESSION_ALREADY_ACTIVE`。

### 4.5 `reset-prng`

只允许在成功 warm-up 后的 `PRNG_RESET_REQUIRED` 状态执行。输入 seed 必须与 trial root seed 一致。
成功后进入 READY。

### 4.6 `close-session`

只有持有匹配 session ID 的 orchestrator 可以正常关闭。关闭时必须确认没有 in-flight request。

### 4.7 `end-trial`

要求 session 已关闭，并将请求数、首尾哈希、错误状态和证据目录原子提交。若审计提交失败，trial
不能报告成功。

### 4.8 `abort`

撤销 session、拒绝新 inference、等待或强制标记 in-flight 请求为 ambiguous，并关闭 trial。
`abort` 不发布机器人动作；Edge 负责先停止本地 publisher。

## 5. 推理请求

每个 binary request 必须是以下 envelope，不存在 legacy fallback：

```json
{
  "protocol_version": 1,
  "message_type": "inference_request",
  "session_id": "UUID",
  "session_token": "secret",
  "trial_id": "run_stage_attempt",
  "request_kind": "infer",
  "expected_skill": "open_door",
  "expected_generation": 4,
  "sequence": 1,
  "sent_at_monotonic_ns": 123456789,
  "observation": {}
}
```

必需验证顺序：

1. binary frame、大小和可解码性；
2. protocol version 和 message type；
3. 所有必需字段存在且类型正确；
4. session ID 和 constant-time token 比较；
5. session 未过期；
6. trial ID 匹配；
7. expected skill 匹配；
8. expected generation 匹配；
9. request kind 与 model state 匹配；
10. sequence 等于 session 期望的下一个值；
11. inference lock 可按有界 timeout 获得。

任一步失败都不能调用 policy。

### 5.1 Warm-up

Warm-up 使用相同 envelope，但：

```json
"request_kind": "warmup",
"sequence": 0
```

服务端执行真实 policy inference 和审计，但 response 标记 `publishable=false`。Edge 的类型系统和
Action Executor 还必须再次拒绝任何 warm-up action。

成功后 server 状态从 WARMUP_REQUIRED 转为 PRNG_RESET_REQUIRED。

### 5.2 Rollout inference

PRNG reset 后，第一个 rollout 请求从 `sequence=1` 开始。每个请求严格递增，不允许跳号、重复或
乱序。

完整临界区：

```text
acquire inference lock
→ validate state again
→ split/generate noise
→ increment request index
→ policy.infer
→ validate action
→ append audit record
→ build response
→ release inference lock
```

## 6. 推理响应

成功 response：

```json
{
  "protocol_version": 1,
  "message_type": "inference_response",
  "session_id": "UUID",
  "trial_id": "run_stage_attempt",
  "skill": "open_door",
  "generation": 4,
  "sequence": 1,
  "publishable": true,
  "request_hash": "sha256",
  "noise_hash": "sha256",
  "action_hash": "sha256",
  "server_timing_ms": {},
  "actions": []
}
```

Edge 在发布前必须验证所有回显字段与请求一致。任何 mismatch 都触发 SAFE_STOP。

## 7. 稳定错误代码

至少定义：

```text
PROTOCOL_VERSION_UNSUPPORTED
INVALID_MESSAGE_TYPE
MISSING_REQUIRED_FIELD
INVALID_FIELD_TYPE
FRAME_TOO_LARGE
SESSION_REQUIRED
SESSION_INVALID
SESSION_EXPIRED
SESSION_ALREADY_ACTIVE
TRIAL_MISMATCH
SKILL_MISMATCH
GENERATION_MISMATCH
SEQUENCE_MISMATCH
STATE_REJECTED
INFERENCE_BUSY
INFERENCE_TIMEOUT
POLICY_FAILED
ACTION_INVALID
AUDIT_COMMIT_FAILED
SERVER_FAILED
```

错误 response 不包含 traceback、路径、token 或 observation 内容。详细 traceback 只写 Surf 本地日志。

## 8. 超时与重试

### 8.1 可安全重试

- `status`；
- tunnel health check；
- 在有明确幂等 key 时查询已提交的 trial evidence。

### 8.2 不可自动重试

- warm-up inference；
- rollout inference；
- PRNG reset；
-物理复位；
- action 发布。

如果 request 已发送但 response 未确认，当前 trial 标记 `AMBIGUOUS_INFERENCE`。后续必须 abort
session，禁止把相同 sequence 自动重发。

## 9. 协议版本策略

首版只接受 `protocol_version=1`。未来新版本若无法完全保持安全语义，应使用新端口或显式配置，
不得在同一 handler 中加入“缺字段也尝试执行”的兼容分支。

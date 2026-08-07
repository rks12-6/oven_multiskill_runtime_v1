# 安全不变量

状态：活跃安全契约。以下规则优先于性能和兼容性；已实现组件必须有自动化回归测试，未实现项仍是后续验收门。

## S-001：所有 inference 请求必须完整认证

服务端只接受包含有效 session ID、secret token、trial ID、skill、generation、sequence 和协议版本的
envelope。裸 observation、旧协议和缺字段请求必须在调用 policy 前拒绝。

验证：协议测试向服务端发送每一种缺字段组合，断言 policy 调用计数保持为 0。

## S-002：Skill 与 generation 必须双重匹配

请求声明的 Skill 和 generation 必须同时匹配 active policy 与 session 绑定值。任何一个不匹配都拒绝。

验证：切换 Skill 后重放旧请求，断言返回 `GENERATION_MISMATCH` 或 `SESSION_INVALID` 且无 inference。

## S-003：同一时刻最多一个动作 session

服务端只允许一个 ACTIVE lease。Edge 同时只允许一个 Action Executor 持有本地 publisher lease。
两端任一租约获取失败都禁止 rollout。

验证：两个客户端并发 open-session，恰好一个成功。

## S-004：有状态 policy inference 必须串行

从 PRNG split 到 action 审计提交的完整过程由同一互斥锁保护。不能只保护计数器或 policy 指针。

验证：并发发送两个请求，记录临界区最大并发数必须为 1，sequence/noise hash 顺序稳定。

## S-005：状态不允许时不得调用 policy

Warm-up 只允许在 WARMUP_REQUIRED；正常 inference 只允许在 READY；DRAINING、SWITCHING、LOADING、
FAILED 和 STOPPING 全部拒绝。

验证：对每个状态执行 warm-up 和 infer 的参数化测试。

## S-006：Warm-up action 永不发布

Warm-up response 必须标记 `publishable=false`。Edge 不得把 warm-up response 类型传给 Action Executor；
即使错误调用，Action Executor 也必须拒绝。

验证：fake robot 的 publish 计数在 warm-up 后仍为 0。

## S-007：切换 Skill 必须撤销旧 session

进入 DRAINING 前先禁止新请求并撤销 session。Generation 更新后，旧连接和旧 token 永久失效。

验证：切换期间和切换后分别重放旧请求，两次都不得进入 policy。

## S-008：网络和控制面默认不可被局域网直接访问

Surf inference 只绑定 loopback，控制使用 `0600` Unix socket。任何非 loopback TCP 监听都使 server
启动失败。

验证：启动测试检查实际 socket address，并扫描配置中禁止的 bind host。

## S-009：Action 在发布前必须验证

Action Executor 必须验证：

- response session、trial、skill、generation、sequence 与请求一致；
- `publishable=true`；
- action shape 与配置一致；
- 所有数值有限；
- 关节位置、速度、单步变化和总时长在限制内；
- observation 和 response 未超过 freshness deadline；
- publisher lease 仍有效。

任一失败进入 SAFE_STOP。

## S-010：Observation 必须完整且新鲜

必需相机和关节 topic 都必须存在、时间戳处于窗口内、维度正确且来源 ROS domain 匹配。不能用同一
图像静默代替缺失摄像头，除非 Skill 配置明确声明并在 manifest 中记录。

验证：缺 topic、旧时间戳、错误 shape 和重复替代分别失败。

## S-011：每个外部操作必须有有界时间

SSH、模型加载、inference、reset、Checker、drain、monitor 和 publisher shutdown 都必须配置 timeout。
timeout 后不得继续下一个阶段。

验证：fake dependency 永不返回，orchestrator 在上限内进入 SAFE_STOP。

## S-012：含糊 inference 不得自动重试

请求已发送但 response 丢失时，客户端不知道 server 是否消耗了 PRNG 或生成了 action。必须将 trial
标记 ambiguous、abort session，禁止重发相同 sequence。

验证：在 policy 完成后丢弃 response，确认客户端不发第二次请求。

## S-013：Checker 与 Joint Gate 都是强制门

阶段成功必须同时具备：批准的 rollout end reason、Joint Gate pass、Checker pass、server audit commit
和 edge evidence commit。退出码 0 本身不构成成功。

验证：逐个移除证据字段，stage 均不得进入 HANDOFF。

## S-014：审计证据必须与 trial 强关联

所有证据路径由 run ID、stage、attempt 和 trial ID 精确构造。不得扫描共享目录的“最新 JSON”。写入采用
临时文件 + fsync + 原子 rename；已有 trial 目录不得覆盖。

验证：并发 trial、相同 ID、审计写失败和进程中断测试。

## S-015：异常必须先停止动作，再清理远端状态

SAFE_STOP 顺序固定：

1. 阻止新 action；
2. 停止并确认 publisher；
3.记录本地停止时间；
4. abort 远端 session/trial；
5. 停止 monitor；
6. 原子提交失败 manifest。

远端不可达不能阻止本地 publisher 停止。

## S-016：真实机器人必须显式确认

Plan、doctor、fake robot 和 observation-only 不需要真机 token。创建动作 publisher 前必须同时满足：

- CLI `--execute-real-robot`；
- 精确环境确认 token；
- 交互式现场确认未超时；
- preflight pass；
- 当前 run manifest 已落盘。

任何一个缺失都不得创建 publisher。

## S-017：旧项目不能成为隐式运行依赖

运行时不得 import、source、exec、runpy 或 subprocess 调用旧 `skills/`、旧 `labs/` 代码。配置验证必须
拒绝指向这些目录的可执行路径。

验证：源码扫描、配置扫描和在旧目录不可访问的环境中运行完整 fake integration test。

## S-018：配置声明必须转化为观测事实

Manifest 分开保存 `requested` 和 `observed`。后端、seed、generation、asset hash、监听地址和 Checker
版本必须由运行组件回报并核对，不能把配置值直接当作实验事实。

验证：fake server 故意报告不同 seed/backend，orchestrator 必须停止。

## S-019：失败状态不能被覆盖为 completed

Execution、Checker、audit、timing 分别记录状态。`completed` 只能由纯函数根据必需证据计算，finally
清理逻辑不能覆盖之前的失败状态。

验证：在每个阶段注入异常，最终 manifest 保留准确失败原因。

## S-020：资产和代码版本必须可追溯

每次 run 必须记录：

- 两端 Git commit；
- dirty 状态；
- Python 和关键依赖版本；
- checkpoint/base/adapter/Checker SHA256；
-有效配置快照；
- server instance ID。

缺少必需版本证据时可以进行诊断，但不能作为正式可比较 trial。

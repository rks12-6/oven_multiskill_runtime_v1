# 验收测试计划

状态：活跃验收计划。L1/L2 安全核心和 L3 首批传输场景已有 37 项测试；其余场景仍未完成。

## 1. 测试层级

```text
L0 静态与配置
L1 纯单元测试
L2 协议与并发测试
L3 双进程集成测试
L4 双机但无 GPU/ROS 动作测试
L5 Surf 真实模型、Agilex observation-only
L6 fake robot 全流水线
L7 受监督单 Skill 真机
L8 受监督三 Skill 真机
L9 故障注入与恢复
```

正式真机运行要求 L0–L6 全部通过并保存测试报告。

## 2. L0：静态、依赖与配置

必须通过：

- formatter、lint、类型检查；
- Python package 可在干净环境安装；
- dependency lock 可重复安装；
- TOML schema 和所有 profile 校验；
- 仓库源码不含旧 `skills/`/`labs/` 可执行依赖；
- 项目代码不含机器散布式绝对路径；
- secret 不进入 Git；
- server 配置只允许 loopback bind；
-资产清单 SHA256 与实际文件一致。

## 3. L1：纯单元测试

覆盖：

- 合法与非法状态转换；
- path root 解析和逃逸拒绝；
- observation/action shape 校验；
- action limit；
- Joint Gate 窗口、spread 和 tolerance；
- Checker 结构化结果；
- manifest 完成状态计算；
- 哈希包含 path、dtype、shape 和 value；
- timeout 和取消错误分类；
- trial ID、run ID 和 asset ID 校验。

目标：核心纯逻辑分支覆盖率不低于 90%。覆盖率不是替代场景测试的完成标准。

## 4. L2：协议与 P0 回归测试

### 4.1 身份绕过测试

以下请求全部拒绝且 policy call count 为 0：

- 裸 observation；
- 缺 session token；
- 错误 token；
- 缺 expected skill；
- 错误 expected skill；
- 缺 generation；
- 旧 generation；
- 未知 protocol version；
- text frame；
- 超大 frame；
- trial mismatch；
- 重复 sequence；
- 跳跃 sequence。

### 4.2 并发测试

- 100 个并发 inference 请求的临界区最大并发数为 1；
- 两个 session 竞争时只有一个成功；
- infer 与 switch 竞争时不发生旧 policy action 泄漏；
- infer 与 reset-prng 竞争时只有合法状态操作成功；
- drain timeout 后进入 FAILED，不能继续切换；
- warm-up 与普通 inference 不可并行。

### 4.3 重放与断线

- 切换后重放旧请求；
- server 重启后重放旧 token；
- policy 完成后丢弃 response；
- response 只发送一半后断线；
- SSH tunnel 中断；
- client 进程被 SIGKILL。

都必须保持 fail-closed，不自动重复 action。

## 5. L3：本机双进程集成测试

使用 fake policy server 和 fake edge client：

```text
prepare open_door
→ warm-up
→ reset PRNG
→ 3 次 infer
→ joint gate pass
→ checker pass
→ close session
→ commit trial
→ switch transport_food
```

验证两端 evidence 的 session、generation、sequence 和哈希完全对应。

故障场景：

- model prepare 失败；
- audit 磁盘满；
- Checker 超时；
- fake robot publisher 失败；
- observation 过期；
- malformed action；
- monitor 无法停止；
- manifest 原子提交失败。

## 6. L4：双机无动作测试

在 Agilex 和 Surf 建立真实 SSH 隧道，但使用 fake policy/recording executor：

- Surf 只监听 loopback；
- LAN 直接连接失败；
- SSH 隧道健康检查通过；
- forced-command 或 serverctl 参数限制有效；
- 两端时钟信息被记录，但阶段耗时使用 monotonic clock；
-中断隧道触发本地 SAFE_STOP；
-旧项目目录临时不可访问时测试仍通过。

## 7. L5：真实模型、无动作发布

逐个后端验证：

- Full Checkpoint 加载三种 Skill；
- Composite Adapter 加载三种 Skill；
- 相同 observation + root seed 的 action hash 可重复；
- warm-up 后 PRNG reset 的第一个 action 与冷启动基线符合预定比较规则；
-切换前后 generation 正确改变；
- GPU 内存和模型引用符合单 active policy 约束；
- action shape、dtype 和数值范围通过；
-服务端审计保存首个 action chunk。

本层 Action Executor 使用 recording sink，禁止 ROS publisher。

## 8. L6：Fake robot 全流水线

使用录制或合成 observation、fake reset、fake publisher 和 deterministic Checker 跑完整三 Skill：

- 正常三 Skill 完成；
- 每个 Skill Checker fail；
- 每个 Skill Joint Gate fail；
- max-step 允许与不允许两种结束；
- stage handoff 证据缺失；
- 第二个 action client 冲突；
-用户 Ctrl+C；
-服务端在第二个 Skill 前崩溃。

测试最终 manifest 不允许出现“失败后被 finally 改写为 completed”。

## 9. L7：受监督单 Skill 真机

每个 Skill 独立完成以下门：

1. observation-only；
2. reset-only；
3. warm-up，无 publisher；
4. 单次 inference，action 只记录；
5. 低 max-step rollout；
6. 完整 Joint Gate；
7. Checker；
8. 重复至少三次并比较 evidence。

每一步需要新的人工确认。测试人员必须能够物理急停。

## 10. L8：受监督三 Skill 真机

进入条件：三个单 Skill 都通过，且审查者批准阶段间姿态和 Checker 规则。

验收要求：

- Open Door、Transport Food、Close Door 顺序不可配置错误；
-阶段间不依赖人工修改日志或共享目录最新文件；
-每个 Skill 有独立 trial evidence；
-三次完整运行无安全停止；
-至少一次 Checker 故意失败能够阻止下一 Skill；
-至少一次旧 generation 请求注入被服务端拒绝；
-完整运行可根据 Git commit、配置和资产哈希复现。

## 11. L9：故障注入

在不危及硬件的条件下验证：

-推理前断 SSH；
-推理响应期间断 SSH；
-Checker 进程崩溃；
-Joint topic 停止；
-相机 topic 停止；
-Surf 磁盘只读或审计写失败；
-Agilex 审计写失败；
-server 进入 FAILED；
-action publisher shutdown 超时。

所有场景必须先完成本地动作停止，再处理远端清理。

## 12. 完成定义

“实现完成”要求：

- L0–L6 自动化测试通过；
-代码、协议和安全不变量完成逐行审查；
-两端部署文档能从干净 checkout 执行；
-没有旧项目运行依赖；
-两个 P0 回归测试存在并通过；
-安全负责人批准进入 L7。

“真机验证完成”要求额外通过 L7–L9，并由现场操作者签署运行记录。软件实现完成不能替代真机验证完成。

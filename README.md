# Oven Multi-Skill Runtime v1

这是 Open Door → Transport Food → Close Door 的全新双机运行时项目。

当前仓库已进入 **Stage 1/2：安全核心实现**。已经存在纯 Python 协议、状态机、审计事务、
fake policy runtime 和回归测试；仍然没有连接 ROS、GPU 或真实机器人的执行入口。

运行与部署目标仅包括 Agilex Linux 和 Surf Linux。Windows 只可作为编辑暂存环境，不属于运行平台，
项目不提供 Windows 路径、服务、socket 或 signal 兼容层。

## 项目目标

- Agilex 负责 ROS observation、物理复位、动作发布、Joint Gate、Checker 和流水线编排。
- Surf 负责模型加载、Skill 切换、串行推理、PRNG 管理和服务端审计。
- 两端使用同一个 Git 仓库和同一份协议定义，避免客户端与服务端协议分叉。
- 运行时不调用任何旧 `skills/` 或 `labs/` 目录中的代码。
- 项目内路径相对仓库根目录；checkpoint、资产和运行数据只通过少量可配置根目录定位。
- 所有真实动作都必须经过显式确认、状态检查和单客户端租约。

## 设计文档

- `docs/architecture.md`：系统边界、组件、状态机、调用链和部署结构。
- `docs/protocol.md`：控制协议、推理请求、会话租约、错误语义和重试规则。
- `docs/safety_invariants.md`：任何实现都不得破坏的安全不变量。
- `docs/acceptance_tests.md`：从纯单元测试到受监督真机测试的验收标准。

## 明确不依赖的旧代码

运行时不得 import、执行、source 或动态加载以下目录中的文件：

```text
/home/agilex/skills/
/home/agilex/labs/
/home/surf2026/openpi/labs/
```

本项目自身部署目录除外。旧代码只能用于人工理解历史行为，不能成为运行依赖。

OpenPI、ROS、checkpoint、摄像头驱动和模型权重属于平台或数据依赖。它们必须通过正式配置、
版本信息和 SHA256 清单声明，不能通过旧脚本间接获得。

## 计划部署位置

```text
Agilex: /home/agilex/labs/oven_multiskill_runtime_v1
Surf:   /home/surf2026/openpi/labs/oven_multiskill_runtime_v1
```

两个目录应是同一仓库版本的独立 checkout。运行数据不提交 Git。

## 当前禁用事项

在 fake-policy、双机无动作和 fake-robot 测试通过前，不允许：

- 启动或停止 Surf 上的任何 policy server；
- 建立推理隧道；
- 订阅真实 observation；
- 发布机器人 action；
- 执行物理复位；
- 迁移或覆盖 Checker 模型。

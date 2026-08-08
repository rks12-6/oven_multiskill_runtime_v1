# Oven Multi-Skill Runtime v1

一套已经完成真机四技能串联的双机运行时：

```text
open_door → transport_food → close_door → rotate_button
```

- AgileX：ROS、相机、机器人复位与动作发布、Joint Rest Gate、Checker、流程编排。
- 5090：OpenPI shared base、LoRA adapter 切换、推理与服务端审计。
- 运行时代码独立，不 import、source 或执行旧 `skills/`、`labs/` 项目的代码。
- checkpoint、adapter 和 checker 是外部数据资产，通过配置根目录定位，不复制进仓库。

## 当前状态

2026-08-08 已在真机完成四技能串联：

```text
run_id: 20260808_160331_agilex_oven_v1
status: COMPLETED
completed_stages: 4
```

相较此前 `inference_eval_v2` 的标准执行路径，完整串联时间缩短到约原来的 **2/3**。当前版本保留一个常驻
5090 server 和 shared base，每个技能只切换 adapter；不再执行丢弃结果的 warm-up，也不再要求每阶段重复授权。

另外两次现场运行分别在 `open_door` 和 `close_door` 耗尽动作预算后正确停止。失败后 5090 保持
`READY`，无需重启服务。这类策略/场景稳定性问题暂不通过放宽 gate 或把 `max_steps` 当成功来掩盖。

## 运行语义

一次完整运行只授权一次：

```text
YES RUN <自动生成的 run_id>
```

每个技能执行：

```text
可选机器人复位
→ settle 3 秒
→ 切换 adapter
→ begin trial（root_seed=0，同时初始化 PRNG）
→ sequence=0 的第一份推理结果直接正式执行
→ 发布动作，直到 joint_rest_detected
→ 提交服务端 trial
→ Joint Gate
→ Checker（rotate_button 不需要）
→ 下一技能
```

动作契约：

- OpenPI 可以返回 `[50, 7]` 或 ALOHA wrapper `[50, 14]`。
- 四个单臂 policy 的有效输出均为前 7 维；技能配置决定发布到左臂还是右臂。
- 不额外修改 gripper 数值。
- 每个 chunk 必须恰好 50 行。
- 四技能动作预算分别为 600、600、400、1000 行。
- 未检测到 `joint_rest_detected` 时，耗尽预算是失败，不是成功。

## 部署位置

```text
AgileX: /home/agilex/labs/oven_multiskill_runtime_v1
5090:   /home/amax/openpi/labs/oven_multiskill_runtime_v1
```

运行数据不进入 Git：

```text
AgileX: /home/agilex/oven_runtime_v1
5090:   /home/amax/oven_runtime_v1
Assets: /home/agilex/oven_assets_v1
```

当前 5090 资产根目录：

```text
artifact root:   /home/amax/openpi/labs/pi05_inference_audit_server_v1/artifacts
checkpoint root: /data/cobot_magic/openpi/checkpoints
```

这里复用的是已经验证过的模型数据资产，不是旧项目运行时代码。

## 日常真机运行

5090 server 常驻后，每次实验只需操作 AgileX。人工恢复烤箱门、食物和周围场景后执行：

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1

cd /home/agilex/labs/oven_multiskill_runtime_v1

PYTHONPATH="$PWD/src:$PYTHONPATH" \
  /home/agilex/miniforge3/envs/aloha/bin/python \
  -m oven_runtime.edge.app \
  --profile config/agilex_oven_v1.toml \
  --runtime-root /home/agilex/oven_runtime_v1 \
  --asset-root /home/agilex/oven_assets_v1 \
  --execute
```

程序打印 run plan 后，输入它要求的完整 `YES RUN <run_id>`。随后自动建立 SSH tunnel、依次执行四技能并清理
session/trial。`--run-id` 可省略，由程序自动生成。

注意：`PYTHONPATH` 必须在项目源码后保留原值，否则会覆盖 ROS 提供的 `rclpy` 路径。

## 无动作检查

查看计划，不连接 ROS、不发布动作：

```bash
PYTHONPATH="$PWD/src:$PYTHONPATH" \
  /home/agilex/miniforge3/envs/aloha/bin/python \
  -m oven_runtime.edge.app \
  --profile config/agilex_oven_v1.toml \
  --runtime-root /home/agilex/oven_runtime_v1 \
  --plan
```

只采集四技能 observation，不创建 action publisher：

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1

PYTHONPATH="$PWD/src:$PYTHONPATH" \
  /home/agilex/miniforge3/envs/aloha/bin/python \
  -m oven_runtime.edge.app \
  --profile config/agilex_oven_v1.toml \
  --runtime-root /home/agilex/oven_runtime_v1 \
  --observe-once
```

## 5090 server

静态检查模型资产，不启动推理：

```bash
cd /home/amax/openpi/labs/oven_multiskill_runtime_v1

PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" \
  /home/amax/openpi/.venv/bin/python \
  -m oven_runtime.server.preflight \
  --backend-profile config/inference_5090_v1.toml \
  --artifact-root /home/amax/openpi/labs/pi05_inference_audit_server_v1/artifacts \
  --checkpoint-root /data/cobot_magic/openpi/checkpoints
```

常驻服务启动命令：

```bash
nohup env PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" \
  /home/amax/openpi/.venv/bin/python \
  -m oven_runtime.server.app \
  --backend openpi-composite \
  --backend-profile config/inference_5090_v1.toml \
  --artifact-root /home/amax/openpi/labs/pi05_inference_audit_server_v1/artifacts \
  --checkpoint-root /data/cobot_magic/openpi/checkpoints \
  --runtime-root /home/amax/oven_runtime_v1 \
  --host 127.0.0.1 \
  --port 19110 \
  --ready-file ready/server.json \
  >> /home/amax/oven_runtime_v1/logs/server_restart.log 2>&1 \
  </dev/null &

new_pid=$!
printf '%s\n' "$new_pid" > /home/amax/oven_runtime_v1/server.pid
echo "server pid=$new_pid"
```

查询状态：

```bash
PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" \
  /home/amax/openpi/.venv/bin/python \
  -m oven_runtime.server.control_cli \
  --socket /home/amax/oven_runtime_v1/control/oven-server.sock \
  status
```

空闲时正常状态为 `STARTING`（尚未加载第一个技能）或 `READY`；必须同时满足：

```text
active_session_id = null
active_trial_id = null
in_flight = 0
last_error = null
```

## 故障边界

- Checker 拒绝、Joint Gate 未通过、用户中断和普通 rollout 失败只终止当前 trial，服务回到 `READY`。
- 模型执行失败、审计提交失败或推理响应是否送达无法确定时，服务进入 `FAILED`，必须人工检查后重启。
- 失败后不要直接重复整套运行；机器人 reset 不会自动恢复烤箱门、食物和场景布局。
- 不要为了提高表面成功率，把动作预算耗尽改成成功。

## 代码与文档入口

- `src/oven_runtime/edge/orchestrator.py`：四技能主流程。
- `src/oven_runtime/edge/ros2_observation.py`：ROS observation。
- `src/oven_runtime/edge/ros2_action.py`：唯一机器人动作 publisher。
- `src/oven_runtime/server/runtime.py`：trial/session/推理生命周期。
- `src/oven_runtime/server/composite_backend.py`：shared base 与 adapter 切换。
- `config/agilex_oven_v1.toml`：机器人、动作预算、gate、checker。
- `config/inference_5090_v1.toml`：四技能 checkpoint/adapter manifest。
- `docs/minimal_runtime.md`：当前最小运行语义。

`docs/architecture.md`、`docs/protocol.md` 和 `docs/acceptance_tests.md` 中仍有早期三技能/warm-up 设计记录，暂作为历史材料，
不应覆盖本 README、`docs/minimal_runtime.md` 和当前代码的实际语义。

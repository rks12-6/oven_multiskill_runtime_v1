# Right Full-HITL Rotate Button: human-supervised regression

This procedure is for the current v2 profile binding:
`rotate_button -> right_hitl -> right_reset -> full_hitl`.

It is intentionally a real-robot procedure. Read every warning before running a command. Do not use
`/hitl/manual_takeover` during the POLICY portion: this regression specifically validates physical
right-rear Teach takeover.

## 0. Physical state before any command that can move hardware

```text
LEFT FRONT:  Power ON, Teach OFF
LEFT REAR:   Power OFF
RIGHT FRONT: Power ON, Teach OFF
RIGHT REAR:  Power ON, Teach OFF
3 cameras:   ON
```

Support the right arms while enabling or resetting. Clear the workspace. Keep an operator at the
physical emergency-stop. Do not start a second right-front Piper node.

## 1. One-time CAN preparation

Run only if `can_left`, `can_right`, and `can_right_rear` are not already configured. This changes
CAN interfaces but does not enable an arm or publish a joint command.

```bash
cd /home/agilex/piper_ros && bash ./can_config_hitl_right.sh
```

Expected interfaces:

```text
can_left       -> 1-13:1.0
can_right      -> 1-12.1:1.0
can_right_rear -> 1-12.3:1.0
```

## 2. Terminal 1 — left legacy Piper

This long-running launch enables the mature left-front legacy path. It can make the left arm stiff;
it does not send a policy trajectory by itself.

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 launch piper start_left_legacy.launch.py can_left_port:=can_left auto_enable:=true
```

Pass: `/piper_left_ctrl_node` and `/puppet/joint_left` appear in domain 222.

## 3. Terminal 2 — right Full-HITL Piper

This launch starts the sole right-front driver, sole right-rear owner, and `hitl_arbiter`. Both
right arms remain disabled until Terminal 4. Keep `auto_takeover_from_teach:=false`: direct
POLICY physical Teach takeover is handled by the full-HITL state machine, not by legacy HOLD auto
takeover.

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 launch piper_hitl start_right_hitl.launch.py front_can_port:=can_right rear_can_port:=can_right_rear front_auto_enable:=false rear_auto_enable:=false auto_takeover_from_teach:=false
```

Pass: `/piper_right_ctrl_node`, `/piper_right_rear_ctrl_node`, and `/hitl_arbiter` appear. Do not
start `piper_single_ctrl` for `can_right_rear` separately.

## 4. Terminal 3 — three cameras

The v2 contract requires these topics:

```text
/camera_l/color/image_raw
/camera_r/color/image_raw
/camera_f/color/image_raw
```

The supported camera contract is the attached **Orbbec triple**. Terminal 4 preflight is the
authoritative topic/freshness confirmation.

```bash
source /opt/ros/humble/setup.bash && source /home/agilex/camera_ros/install/setup.bash && export ROS_DOMAIN_ID=222 ROS_LOCALHOST_ONLY=1 && conda deactivate && cd ~/camera_ros/scripts/ && bash start_orbbec_camera.sh
```

## 5. Terminal 4 — read-only ROS graph preflight, then explicit right enables

Camera startup intentionally precedes graph preflight: the preflight fails closed unless all three
camera streams are fresh. It checks domain 222, all required nodes, fresh left/right/right-rear
feedback, all three camera topics, `/hitl/state=HOLD`, final-command publisher ownership, and
enable-service ownership. Any failure means **DO NOT RUN ROBOT**.

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
bash /home/agilex/piper_ros/src/piper_hitl/tools/check_hitl_graph.sh
```

Only after that command passes, enable each right arm. These two commands can make an arm stiff or
move slightly; support the arms. They do not start POLICY.

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 service call /hitl/right_front/enable_srv piper_msgs/srv/Enable "{enable_request: true}"
```

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 service call /hitl/right_rear/enable_srv piper_msgs/srv/Enable "{enable_request: true}"
```

## 6. Terminal 5 — Amax READY and v2 tunnel probe

This terminal is intentionally not a persistent `ssh -L` process. The current `edge.app` owns the
live `127.0.0.1:19110` tunnel and will refuse to proceed if it cannot connect. A separate long-lived
tunnel would collide with that port.

First query the remote control server. This is a read-only `status` request and must report an
`ok` response with remote state `READY` (or the documented idle startup state).

```bash
ssh -o BatchMode=yes oven-inference /home/amax/openpi/.venv/bin/oven-serverctl --socket /home/amax/oven_runtime_v1/control/oven-server.sock --timeout-sec 20 status
```

Then probe the exact v2 tunnel implementation. It opens `127.0.0.1:19110`, verifies TCP
connectivity, prints success, and closes immediately; it does not contact ROS or issue robot
commands.

```bash
conda activate aloha
cd /home/agilex/labs/oven_multiskill_runtime_v2
unset PYTHONPATH
export PYTHONPATH="/opt/ros/humble/local/lib/python3.10/dist-packages:/opt/ros/humble/lib/python3.10/site-packages:$PWD/src"
python -c "from oven_runtime.edge.tunnel import SshInferenceTunnel; tunnel=SshInferenceTunnel(host_alias='oven-inference', local_port=19110, remote_port=19110); tunnel.start(); print('Amax tunnel probe OK'); tunnel.close()"
```

Any failure here means **DO NOT RUN ROBOT**. Do not leave a manual `ssh -L` tunnel running.

## 7. Terminal 6 — v2 runtime and the real rollout

This terminal creates the live tunnel itself, runs observation/HITL preflight, runs the configured
right-front/right-rear reset, returns to HOLD, and only then starts POLICY. Reset causes real arm
motion.

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
conda activate aloha
cd /home/agilex/labs/oven_multiskill_runtime_v2
unset PYTHONPATH
export PYTHONPATH="/opt/ros/humble/local/lib/python3.10/dist-packages:/opt/ros/humble/lib/python3.10/site-packages:$PWD/src"
export OVEN_RUNTIME_ROOT=/home/agilex/oven_runtime_v2
export OVEN_ASSET_ROOT=/home/agilex/oven_assets_v1
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
which python
python -c "import rclpy, oven_runtime; print('rclpy OK'); print('oven_runtime =', oven_runtime.__file__)"
python -m oven_runtime.edge.app --profile config/agilex_oven_v1.toml --hitl-v2-profile config/hitl_v2.toml --skill rotate_button --runtime-root "$OVEN_RUNTIME_ROOT" --asset-root "$OVEN_ASSET_ROOT" --execute
```

Do not press physical Teach during reset. Observe:

```text
HOLD -> RESETTING -> HOLD -> POLICY
```

Once POLICY is visibly running, press the physical **right-rear Teach ON** button. Do **not** call
`/hitl/manual_takeover`.

Expected sequence:

```text
POLICY
-> physical right-rear Teach ON
-> Piper stops final front/rear POLICY output locally
-> v2 cancels chunk, lease, and policy generation
-> HUMAN
-> move right-rear
-> right-front follows front_anchor + (rear_actual - rear_anchor)
-> physical right-rear Teach OFF
-> HOLD
```

## 8. Read-only observation terminal

Use this terminal while Terminal 6 is running. Every command here is read-only.

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 topic echo /hitl/state
```

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 topic info -v /joint_right_states
```

Pass: publisher count is `1` and the publisher is `hitl_arbiter`.

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 topic echo /hitl/policy_joint_right_cmd
```

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 topic echo /puppet/joint_right
```

```bash
source /opt/ros/humble/setup.bash
source /home/agilex/piper_ros/install/setup.bash
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
ros2 topic echo /puppet/joint_right_rear
```

## 9. Success and abort criteria

Pass requires all of the following:

1. Initial graph preflight passes with `/hitl/state=HOLD`.
2. `rotate_button` produces `HOLD -> RESETTING -> HOLD -> POLICY`.
3. `/joint_right_states` has exactly one publisher, `hitl_arbiter`.
4. POLICY produces right-front execution and right-rear shadow feedback.
5. Physical rear Teach ON, with no software manual-takeover call, reaches `HUMAN`.
6. Rear motion produces relative right-front following with no initial jump.
7. Teach OFF produces `HUMAN -> HOLD`; there is no automatic POLICY resume.

Immediately stop the v2 runtime with `Ctrl+C` and keep the arms supported if any of these occur:

- `/joint_right_states` has more than one publisher;
- `/hitl/state=FAULT`;
- unexpected right-front or right-rear motion;
- reset timeout or abnormal reset pose;
- observation/preflight failure;
- Amax/control contract failure;
- `WAIT_TEACH` or `HUMAN` appears before physical rear Teach ON.

`PolicyCancelled` or v2 `PIPELINE_FAILED` immediately after physical takeover is expected ownership
termination of the old rollout, provided `/hitl/state` is `HUMAN` (then `HOLD` after Teach OFF) and
not `FAULT`. A Piper `/hitl/state=FAULT` is a safety failure, not a successful handoff; do not
restart POLICY automatically.

## 10. Shutdown

After Teach OFF confirms HOLD, stop Terminal 6 with `Ctrl+C`, then stop cameras, right Full-HITL,
and left legacy launches with `Ctrl+C`. Do not kill Piper processes by name and do not power-cycle
an unsupported arm.

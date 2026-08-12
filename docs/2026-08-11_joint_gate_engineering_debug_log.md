# 2026-08-11 Joint Gate Engineering Debug Log

Status: read-only investigation completed. This first batch adds only a read-only sampler. Runtime, profile, TOML, server, model, and adapter files were not modified.

## 2026-08-11 13:49:08 +08:00 - baseline and sampling-tool batch

### Current problem and observed evidence

- There were 12 OMR Auto attempts on 2026-08-09; 8 ended fail-closed with JOINT_GATE_FAILED after 1000 published steps.
- Failed stage directories contain only generic stage_failure.json. They do not contain a terminal GateResult, nearest template, per-joint error, or spread diagnostic.
- OD, TF, and CD call the Checker normally after a Gate pass. RB remains checker=NOT_REQUIRED under the SURF scope decision.
- The primary hypothesis remains that the new OMR measured return posture differs from the old Gate templates. The current failure logs cannot yet locate a specific template, joint tolerance, or window/spread cause.

### Batch hypothesis

First collect an active-return measured window for each Skill, then perform offline template, tolerance, window, and spread analysis. Policy actions must not replace measured joint positions, and 1000 steps must not be treated as success.

### Pre-change HEAD and git status

HEAD=c899e5ccc7b6f547c3079f922701ee0d5fcfd4da

M AGENTS.md
M README.md
M src/oven_runtime/edge/app.py
M src/oven_runtime/edge/orchestrator.py
M src/oven_runtime/edge/profile.py
M src/oven_runtime/server/audit.py
M src/oven_runtime/server/runtime.py

### Pre-change file SHA256

src/oven_runtime/edge/joint_gate.py       a60b587262afea7f1696a8a1c0f816b70228177f442ea784d4c036ae18fde34c
src/oven_runtime/edge/ros2_action.py      846779b525976203859149ea6e7dbe7254f1c776228687ea4eec79459118d894
src/oven_runtime/edge/orchestrator.py     352eb090924d285d522bcc87a0c68db69f817437e0963db02bf69af2640c8acd
src/oven_runtime/edge/checker.py          c476f800d04630011fa3862183820a68c234e394feabb3757c5a3544b81efea1
src/oven_runtime/edge/audit.py            3a17d917552298248abd24490580ab092170f4a79fe9b715a8fe7ed4dc907822
config/agilex_oven_v1.toml                f31f71abfc474bed2ff719caec60601ae0918a6eb982a27bed3775b13decbd59
tests/test_agilex_profile.py              3ba29e8d268a74386a026ed0722b63c592f889c2b84e6948d0bb3e47aee9b644
tests/test_fake_pipeline.py               4ecb6764cc89baed171b2b06ee3beb2cc4a930060d069165b8abf49066bc5e54

### Backup path

This batch added new files only and did not overwrite an existing project file, so no pre-existing file backup was needed. Future runtime/profile edits will be backed up only under:

/home/agilex/labs/oven_multiskill_runtime_v1/backups/20260811_joint_gate_<timestamp>/

### New file in this batch

/home/agilex/labs/oven_multiskill_runtime_v1/tools/record_omr_joint_return_window.py

The tool subscribes to one /puppet/joint_left or /puppet/joint_right JointState topic, records the complete 7D measured position, and writes a new JSON file with open("x"). It creates no publisher, does not call the policy server, and does not start Auto.

### Parameter and template before/after

None. This batch changed no TOML, template, tolerance, window, departure, spread, or Gate logic.

### Template data source and run/sample identifiers

There is no new sample data yet. The tool output retains run_id, trial_id, stage, skill, arm, topic, ROS header timestamp, receive monotonic timestamp, and complete 7D position for later traceability.

### Checks and results

- Read-only target existence, HEAD, dirty status, and baseline SHA256 checks: pass.
- ROS subscriber, server, Auto, robot control, and formal tests have not been run.
- The next step after authorized sampling is offline schema, cluster, and replay analysis.

### Authorization, result, remaining issue, and rollback

- This batch is limited to adding the independent read-only sampler and this debug record.
- Robot sampling, server, formal Auto, and any action publisher require separate explicit authorization.
- The remaining issue is missing measured joint evidence for failed windows; Gate parameters must not be changed from the current logs alone.
- The two new files can be removed independently. Do not use reset or checkout on existing dirty files.

## 2026-08-11 OD template-0 J2 bias adjustment

Approved minimal configuration change after offline analysis of OMR Open Door samples 03-10.

Evidence:
- Samples 03, 04, 07, 08, and 10 completed with joint_gate PASSED and approved rollout_end_reason=joint_rest_detected.
- Samples 05 and 09 exhausted all 1000 published steps with JOINT_GATE_FAILED; this is not a max_chunks success.
- Samples 05 and 09 had stable low-spread return windows but exceeded template-0 J2 tolerance.
- Sample 06 is excluded from calibration because its audit status was ACTION_INVALID and it did not end in a normal return posture.
- Sample 10 was not used to justify min_publish_step changes; only one of five successful runs showed the longer rollout pattern.

Change:
- [skills.open_door.gate] template-0 checked joint index 1: 0.008168140899331402 -> 0.030124383.
- OD tolerance index 1: 0.03 -> 0.032.
- departure_threshold, min_publish_step, window_samples, max_spread, target rows 1/2, Checker, and all non-OD configuration remain unchanged.

Scope and safety:
- OD configuration only. No TF, other Skill, server, model, adapter, action chunk, or training changes.
- Departure-before-return and Gate-before-Checker contracts remain required.
- No robot, server, Auto, or real-run validation was performed by this change.

Backup:
- /home/agilex/oven_runtime_v1/backups/20260811_omr_od_template_bias_prechange/

Patch timestamp: 2026-08-11T15:38:44

## 2026-08-11 TF sample05 operator-approved template addition

Approved minimal TF template change after offline analysis of OMR Transport Food samples 01-08 and explicit operator confirmation that sample05 completed the intended physical TF result and returned safely.

Evidence:
- Edge audits map samples 01-08 to the eight single-Skill TF runs from `20260811_145809_agilex_oven_v1_transport_food` through `20260811_152603_agilex_oven_v1_transport_food`.
- Samples 01-04 and 06-08 completed with `joint_gate=PASSED`, `rollout_end_reason=joint_rest_detected`, Checker pass, and rollout exit code 0.
- Sample05 exhausted all 1000 published steps with `JOINT_GATE_FAILED`, `completed_stages=0`, and no Checker result; this remains a Gate failure, not a max-chunks success.
- Sample05's final 100 measured joint frames had median `[ -0.03628539514895295, 0.0, -0.0020594885173527886, 0.14238396037766143, 0.25480061749858784, -0.0768817535503308, 0.0022 ]` and checked-joint maximum spread 0.0.
- Exact offline replay of the current Gate algorithm showed that the original eight TF targets did not match sample05. The same replay preserved all seven prior passes.
- The operator explicitly confirmed sample05 as a physically correct TF result and authorized adding its measured stable-return template.

Change:
- Appended the operator-approved sample05 median as TF target index 8 in `[skills.transport_food.gate].targets`.
- TF tolerance remains `[0.08, 0.05, 0.05, 0.08, 0.08, 0.05]`.
- `departure_threshold=0.10`, `min_publish_step=100`, `window_samples=100`, and `max_spread=0.05` remain unchanged.
- No Checker, Gate logic, server, model, adapter, action chunk, training, or non-TF configuration changed.

Offline validation:
- TOML parse and current Agilex profile load: pass with nine TF targets.
- Exact replay of samples 01-08: pass; sample05 uniquely matched new target index 8, while the prior seven successful samples retained their existing matches.
- `tests.test_agilex_profile`: 3/3 pass, including departure-before-return and stable-return behavior.
- `git diff --check -- config/agilex_oven_v1.toml`: pass.
- The additional legacy `tests.test_fake_pipeline` run reported two pre-existing `StagePlan(max_steps=...)` interface-drift errors; no test or runtime code was changed for that unrelated issue.
- No robot, ROS publisher, server, Auto, or real-run validation was executed by this patch.

Safety and contracts:
- Departure-before-return remains required.
- TF Gate failure still blocks Checker and continuation.
- Checker pass cannot replace Joint Gate pass.
- 1000 published steps and `max_chunks` are not success conditions.

Backup:
- `/home/agilex/oven_runtime_v1/backups/20260811_omr_tf_template_prechange/`
- Pre-change profile SHA256: `719f3258784277fe574ef90812fbfc3652b32cd73a3ba27ebc5e05e2a8369971`.
- Pre-change debug-log SHA256: `f9b3306ab3fe18cb3f6a87f16ae45c72a7f51a1c637c9d7926a6e41ea8ea61be`.

Patch timestamp: 2026-08-11T16:21:42+08:00

## 2026-08-11 OD sample18 J2 return mode

Approved addition of a fourth Open Door joint-gate target based on sample18 terminal evidence.

Evidence:
- OD run: 20260811_165801_agilex_oven_v1_open_door.
- sample18 terminal 200, 500, and 1000 sample windows were identical; max_spread was 0.0.
- Measured terminal 7D: [-0.07030186227031383, 0.082536620326791, -0.0020071286397929725, 0.13493140447164756, 0.16720254234101456, -0.06572909963008985, 0.0013].
- Under the current template-0, only checked joint index 1 exceeded tolerance: absolute error 0.052412 versus tolerance 0.032.
- A shared J2 tolerance of 0.055 caused sample02 to pass and made sample10 hit about 17.9 seconds early in offline replay; it was rejected.
- The narrower J2-only mode preserved template-0 values for all other checked joints. Offline replay kept sample02 and sample06 failing, did not change sample10 timing, and produced a stable sample18 hit.
- After the candidate hit, sample18 remained stable for 23.17 seconds; post-hit maximum joint range was about 0.0406 rad.

Change:
- Appended a fourth target with only template-0 checked joint index 1 moved to 0.082536620326791.
- Tolerance, departure_threshold, min_publish_step, window_samples, max_spread, max_chunks=20, Checker, and existing targets remain unchanged.

Provenance note:
- sample18 JSON metadata is mislabeled as close_door, but its topic is /puppet/joint_left and its capture time overlaps the OD run. The raw trajectory is retained as time-associated OD evidence; the source JSON was not modified.
- sample11 is empty and samples 11-18 carry the same close_door metadata; future OD sampling must use correct skill/stage/trial labels.

Scope and safety:
- OD Gate configuration and this debug log only. No TF, other Skill, server, model, adapter, action chunk, or training changes.
- No robot, server, Auto, or real-run validation was performed after this patch.

Backup:
- /home/agilex/oven_runtime_v1/backups/20260811_omr_od_sample18_j2mode_prechange/

Patch timestamp: 2026-08-11T18:02:33

## 2026-08-12 fast Joint Gate acceptance-range configuration

### Current problem and observed evidence

- The fixed 2026-08-12 audit set contains 76 runs: 45 completed and 31 failed.
- There were 21 `JOINT_GATE_FAILED` runs: 17 exhausted their action-step budget without `joint_rest_detected`, and four failed the old post-rollout handoff evaluation path.
- Open Door also had nine `ACTION_INVALID` failures caused by adjacent policy rows exceeding the configured ROS command delta limits. These are separate from Joint Gate tolerance failures.
- The audit files do not contain failed-run measured joint windows or per-joint terminal diagnostics, so this batch is an operator-selected short-term acceptance-range adjustment rather than a fully sampled recalibration.

### Batch decision and authorization

Red requested an immediately usable configuration intended to raise Joint Gate hit rate and reduce repeated adjacent-row action rejection. Red manually selected the final values and explicitly instructed that differing values should not be repeatedly reconfirmed.

This batch changes configuration only. It does not treat `max_chunks` or action-step budget exhaustion as success, remove departure-before-return, run a Checker after Gate failure, or alter the model, adapter, action chunk, interpolation, or training content.

### Pre-change repository state

```text
HEAD=c899e5ccc7b6f547c3079f922701ee0d5fcfd4da
config/agilex_oven_v1.toml SHA256=ddf6d5500cb5a8704e33b127e5b70d0ef9ad2124875018dae6014442d9bd2ae8
docs/2026-08-11_joint_gate_engineering_debug_log.md SHA256=5bb6437cc8d5a30b4e4f03d68742f3d696fec0a2c4f82d80861720eac06b6a6c
```

The repository already contained unrelated and overlapping dirty changes. They were preserved and excluded from this configuration commit.

### Backup

```text
/home/agilex/labs/oven_multiskill_runtime_v1/backups/20260812_joint_gate_fast_config_20260812_172717/
```

The backup contains the pre-batch TOML, pre-batch debug log, HEAD, git status, and SHA256 list. Rollback must restore only these scoped files; do not use `git reset --hard` or overwrite unrelated dirty work.

### Parameter changes

```text
action.max_row_delta:
  [0.15, 0.15, 0.15, 0.15, 0.15, 0.15, 0.08]
  -> [0.18, 0.18, 0.18, 0.18, 0.20, 0.18, 0.10]

open_door:
  targets: retain the three existing targets, apply the approved J1 target-0 bias,
           and retain the approved fourth J1 return mode
  tolerance: [0.075, 0.03, 0.04, 0.11, 0.075, 0.12]
          -> [0.10, 0.07, 0.065, 0.15, 0.10, 0.16]
  min_publish_step: 200 -> 300
  max_chunks: 20 (unchanged)
  window_samples: 200 (unchanged)

transport_food:
  targets: retain the approved sample05 target as target index 8
  tolerance: [0.08, 0.05, 0.05, 0.08, 0.08, 0.05]
          -> [0.10, 0.07, 0.07, 0.11, 0.11, 0.08]
  min_publish_step: 100 -> 250
  max_chunks: 20 -> 12
  window_samples: 100 (unchanged)

close_door:
  tolerance: [0.065, 0.05, 0.05, 0.14, 0.085, 0.16]
          -> [0.085, 0.07, 0.07, 0.18, 0.11, 0.20]
  min_publish_step: 150 -> 250
  max_chunks: 20 -> 12
  window_samples: 200 (unchanged)

rotate_button:
  tolerance: [0.08, 0.10, 0.03] -> [0.10, 0.13, 0.05]
  max_spread: 0.03 -> 0.05
  min_publish_step: 300, max_chunks: 20, window_samples: 100 (unchanged)
```

All four `departure_threshold` values remain `0.10`. OD/TF/CD remain Checker-required, while RB remains Gate-only with `checker_required=false`.

The four `approved_end_reasons` declarations were intentionally removed as part of the current profile/orchestrator interface change. Red explicitly instructed that they must not be restored. Therefore this configuration commit depends on the existing uncommitted profile/orchestrator interface changes and is not independently cherry-pickable onto the old HEAD.

### Risk boundary

- Wider tolerances increase the chance of accepting a posture that is safe but less precisely returned; negative-window false-positive rate is not established for this emergency configuration.
- The larger `max_row_delta` permits steeper within-chunk adjacent policy rows; it does not smooth them.
- Increasing `min_publish_step` delays eligibility and does not itself improve hit rate.
- `max_chunks` remains a fail-closed budget. Reaching 600 or 1000 steps is not success.
- Successful run status does not establish complete physical task success; rollout, Joint Gate, Checker, terminal result, and system completion remain separate evidence layers.

### Validation boundary

Planned checks for this batch are TOML/profile structural validation in the available environment, exact recorded-window replay for available 2026-08-11 OD/TF/CD samples, `git diff --check`, and staged-diff audit. No robot, ROS publisher, server, edge app, Auto, model, or adapter process is started.

Completed validation:

- Configuration structural checks passed: all Skill gate index/tolerance lengths align, limits are positive, all four `departure_threshold` values remain `0.10`, CD `max_chunks=12`, and the intentional `approved_end_reasons` removal is preserved.
- `git diff --check` passed for the configuration and this debug log.
- Exact terminal-window replay under the selected configuration produced OD 14 pass / 3 fail / 1 empty, TF 8/8 pass, and CD 6/6 pass across the available 2026-08-11 samples. The retained OD failures include clearly non-returned or high-error terminal states; this batch did not broaden the configuration further to force them through.
- RB had no recorded 2026-08-11 joint-window sample available for replay.
- The full project test suite was not claimed: the Agilex system Python environment lacks required project dependencies including `tomli` and `websockets.sync`.
- No real-robot or process execution was performed by this validation.

Patch timestamp: 2026-08-12T17:27:17+08:00

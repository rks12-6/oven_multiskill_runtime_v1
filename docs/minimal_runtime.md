# Minimal four-skill runtime

This document describes the executable baseline. Older design documents may
still describe the discarded warm-up state machine and are not authoritative.

## Stage sequence

Each run is approved once with `YES RUN <run_id>`. Each skill then executes:

1. optional physical reset, followed by the configured 3 second settle;
2. prepare the skill (reuse the shared base and switch the adapter);
3. begin the trial, which initializes policy PRNG from `root_seed`;
4. open one session;
5. request formal inference starting at sequence zero and publish each result;
6. require online `joint_rest_detected` before the action-step budget expires;
7. close the session and commit the server trial;
8. evaluate the final joint gate and, when configured, the checker;
9. hand off to the next skill.

There is no discarded warm-up request and no separate `reset-prng` command.

## Action contract

The established single-arm policies may return either `[50, 7]` or an OpenPI
ALOHA wrapper `[50, 14]`. In both cases the trained local action is columns
`[0:7]`. The edge projects those seven values, and the skill profile selects
the left or right ROS command topic. No extra gripper normalization is applied.

A response with any other row count or dimension is rejected. Stage budgets
are 600, 600, 400, and 1000 published action rows respectively. Exhausting a
budget without `joint_rest_detected` is failure, never successful completion.

## Failure boundary

Operator interruption, checker rejection, joint-gate rejection, and ordinary
edge rollout failure abort only the active trial and leave the inference
runtime reusable in `READY`. Model failures, audit commit failures, or an
ambiguous inference response remain fatal and move the service to `FAILED`.

## Timing baseline

The baseline is deliberately sequential and uses a fresh inference connection
per run while keeping one server process and one shared model base alive. It
does not implement persistent clients, action prefetch, parallel reset/model
load, or speculative warm-up. Timing optimization starts only after this path
has produced a measured four-skill trace.

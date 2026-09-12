#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=/home/agilex/labs/oven_multiskill_runtime_v1
RUNTIME_ROOT=/home/agilex/oven_runtime_v1
ASSET_ROOT=/home/agilex/oven_assets_v1
PYTHON_BIN=/home/agilex/miniforge3/envs/aloha/bin/python
RUN_ID="${1:-$(date +%Y%m%d_%H%M%S)_agilex_oven_v1}"
MEDIA_DIR="$RUNTIME_ROOT/runs/$RUN_ID/media"
VIDEO_PATH="$MEDIA_DIR/three_camera.mp4"
MANIFEST_PATH="$MEDIA_DIR/three_camera.recording.json"
READY_PATH="$MEDIA_DIR/three_camera.ready.json"
RECORDER_LOG="$MEDIA_DIR/three_camera_recorder.log"
RECORDER_PID=

cleanup_recorder() {
  if [[ -n "$RECORDER_PID" ]] && kill -0 "$RECORDER_PID" 2>/dev/null; then
    kill -INT "$RECORDER_PID"
    wait "$RECORDER_PID" || true
  fi
}
trap cleanup_recorder EXIT INT TERM

if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "ERROR: run_id may contain only letters, digits, dot, underscore, and hyphen" >&2
  exit 2
fi
if [[ -e "$VIDEO_PATH" || -e "$MANIFEST_PATH" || -e "$READY_PATH" ]]; then
  echo "ERROR: refusing to overwrite existing recording artifacts for $RUN_ID" >&2
  exit 2
fi

# ROS Humble's generated setup scripts may probe unset AMENT variables.
# Temporarily disable nounset only while sourcing the trusted ROS environment.
set +u
source /opt/ros/humble/setup.bash
set -u
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
mkdir -p "$MEDIA_DIR"
cd "$PROJECT_ROOT"

PYTHONPATH="$PROJECT_ROOT/src:${PYTHONPATH:-}" \
  "$PYTHON_BIN" tools/record_three_cameras.py \
  --run-id "$RUN_ID" \
  --output "$VIDEO_PATH" \
  --manifest "$MANIFEST_PATH" \
  --ready-file "$READY_PATH" \
  >"$RECORDER_LOG" 2>&1 &
RECORDER_PID=$!

deadline=$((SECONDS + 20))
while [[ ! -f "$READY_PATH" ]]; do
  if ! kill -0 "$RECORDER_PID" 2>/dev/null; then
    wait "$RECORDER_PID" || true
    echo "ERROR: three-camera recorder exited before readiness; see $RECORDER_LOG" >&2
    exit 3
  fi
  if (( SECONDS >= deadline )); then
    echo "ERROR: three-camera recorder readiness timed out; see $RECORDER_LOG" >&2
    exit 4
  fi
  sleep 0.1
done

set +e
PYTHONPATH="$PROJECT_ROOT/src:${PYTHONPATH:-}" \
  "$PYTHON_BIN" -m oven_runtime.edge.app \
  --profile config/agilex_oven_v1.toml \
  --run-id "$RUN_ID" \
  --runtime-root "$RUNTIME_ROOT" \
  --asset-root "$ASSET_ROOT" \
  --execute
pipeline_exit_code=$?
set -e

cleanup_recorder
RECORDER_PID=

if [[ ! -s "$VIDEO_PATH" || ! -s "$MANIFEST_PATH" ]]; then
  echo "WARNING: pipeline finished but recording artifacts are incomplete; see $RECORDER_LOG" >&2
fi
exit "$pipeline_exit_code"

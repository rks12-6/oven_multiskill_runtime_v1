#!/usr/bin/env python3
"""Record three ROS 2 Image topics into one labelled H.264 mosaic.

This tool is observation-only: it creates subscriptions and never publishers.
ROS/OpenCV imports stay inside ``main`` so helpers can be tested without ROS.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TOPIC_NAMES = ("front", "left_wrist", "right_wrist")
MOSAIC_ORDER = ("left_wrist", "front", "right_wrist")


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--run-id", required=True)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--manifest", type=Path)
    result.add_argument("--ready-file", type=Path)
    result.add_argument("--front-topic", default="/camera_f/color/image_raw")
    result.add_argument("--left-topic", default="/camera_l/color/image_raw")
    result.add_argument("--right-topic", default="/camera_r/color/image_raw")
    result.add_argument("--fps", type=_positive_float, default=15.0)
    result.add_argument("--tile-width", type=_positive_int, default=640)
    result.add_argument("--tile-height", type=_positive_int, default=480)
    result.add_argument("--preflight-timeout-sec", type=_positive_float, default=15.0)
    result.add_argument("--ffmpeg", default="ffmpeg")
    return result


def output_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    output = args.output.expanduser().resolve()
    manifest = (args.manifest or output.with_suffix(".recording.json")).expanduser().resolve()
    ready = (args.ready_file or output.with_suffix(".ready.json")).expanduser().resolve()
    if len({output, manifest, ready}) != 3:
        raise ValueError("output, manifest, and ready-file must be different paths")
    return output, manifest, ready


def refuse_overwrite(*paths: Path) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite existing path(s): " + ", ".join(existing))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def ffmpeg_command(
    *, executable: str, output: Path, width: int, height: int, fps: float
) -> list[str]:
    return [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-n",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s:v",
        f"{width}x{height}",
        "-r",
        f"{fps:g}",
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]


def letterbox(frame: Any, width: int, height: int, cv2: Any, np: Any) -> Any:
    source_height, source_width = frame.shape[:2]
    if source_width <= 0 or source_height <= 0:
        raise ValueError("frame dimensions must be positive")
    scale = min(width / source_width, height / source_height)
    resized_width = max(1, round(source_width * scale))
    resized_height = max(1, round(source_height * scale))
    resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    x = (width - resized_width) // 2
    y = (height - resized_height) // 2
    canvas[y : y + resized_height, x : x + resized_width] = resized
    return canvas


def compose_mosaic(
    frames: dict[str, Any], *, width: int, height: int, cv2: Any, np: Any, annotation: str = ""
) -> Any:
    tiles = []
    labels = {
        "front": "FRONT",
        "left_wrist": "LEFT WRIST",
        "right_wrist": "RIGHT WRIST",
    }
    for name in MOSAIC_ORDER:
        tile = letterbox(frames[name], width, height, cv2, np)
        cv2.rectangle(tile, (0, 0), (width, 42), (0, 0, 0), thickness=-1)
        cv2.putText(tile, labels[name], (14, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 2)
        tiles.append(tile)
    mosaic = np.concatenate(tiles, axis=1)
    if annotation:
        text_width, _ = cv2.getTextSize(annotation, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)[0]
        x = max(12, mosaic.shape[1] - text_width - 12)
        cv2.rectangle(mosaic, (x - 8, height - 32), (mosaic.shape[1], height), (0, 0, 0), thickness=-1)
        cv2.putText(
            mosaic,
            annotation,
            (x, height - 11),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
        )
    return mosaic


@dataclass
class FrameState:
    frames: dict[str, Any] = field(default_factory=dict)
    source_ns: dict[str, int] = field(default_factory=dict)
    received: dict[str, int] = field(default_factory=lambda: {name: 0 for name in TOPIC_NAMES})
    conversion_errors: dict[str, int] = field(default_factory=lambda: {name: 0 for name in TOPIC_NAMES})
    lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, name: str, frame: Any, source_ns: int) -> None:
        with self.lock:
            self.frames[name] = frame
            self.source_ns[name] = source_ns
            self.received[name] += 1

    def conversion_failed(self, name: str) -> None:
        with self.lock:
            self.conversion_errors[name] += 1

    def snapshot(self) -> tuple[dict[str, Any], dict[str, int]] | None:
        with self.lock:
            if any(name not in self.frames for name in TOPIC_NAMES):
                return None
            return ({name: self.frames[name] for name in TOPIC_NAMES}, dict(self.source_ns))

    def counts(self) -> tuple[dict[str, int], dict[str, int]]:
        with self.lock:
            return dict(self.received), dict(self.conversion_errors)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    output, manifest, ready = output_paths(args)
    try:
        refuse_overwrite(output, manifest, ready)
    except (FileExistsError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        import cv2
        import numpy as np
        import rclpy
        from cv_bridge import CvBridge
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import Image
    except ImportError as error:
        print(f"ERROR: ROS/OpenCV dependency unavailable: {error}", file=sys.stderr)
        return 3

    topics = {
        "front": args.front_topic,
        "left_wrist": args.left_topic,
        "right_wrist": args.right_topic,
    }
    state = FrameState()
    stop = threading.Event()
    started_wall = time.time()
    started_monotonic_ns = time.monotonic_ns()
    frames_written = 0
    max_source_skew_ms = 0.0
    ffmpeg_exit_code: int | None = None
    terminal_reason = "startup_failed"
    process: subprocess.Popen[bytes] | None = None
    executor: Any = None
    node: Any = None

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    class CameraRecorderNode(Node):
        def __init__(self) -> None:
            super().__init__(f"oven_three_camera_recorder_{os.getpid()}")
            self.bridge = CvBridge()
            for name, topic in topics.items():
                self.create_subscription(Image, topic, self._callback(name), qos_profile_sensor_data)

        def _callback(self, name: str) -> Any:
            def receive(message: Any) -> None:
                stamp = message.header.stamp
                source_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
                state.update(name, message, source_ns)

            return receive

    try:
        rclpy.init()
        node = CameraRecorderNode()
        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(node)
        spin_thread = threading.Thread(target=executor.spin, name="three-camera-ros2", daemon=True)
        spin_thread.start()

        deadline = time.monotonic() + args.preflight_timeout_sec
        while not stop.is_set() and state.snapshot() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if stop.is_set():
            terminal_reason = "stopped_before_ready"
            return 130
        preflight_snapshot = state.snapshot()
        if preflight_snapshot is None:
            terminal_reason = "preflight_timeout"
            print("ERROR: timed out waiting for all three camera topics", file=sys.stderr)
            return 4
        preflight_messages, _preflight_source_ns = preflight_snapshot
        try:
            preflight_frames = {
                name: np.asarray(node.bridge.imgmsg_to_cv2(message, desired_encoding="bgr8"), dtype=np.uint8)
                for name, message in preflight_messages.items()
            }
            compose_mosaic(
                preflight_frames,
                width=args.tile_width,
                height=args.tile_height,
                cv2=cv2,
                np=np,
                annotation=f"{args.run_id}  preflight",
            )
        except Exception as error:
            terminal_reason = "preflight_conversion_failed"
            print(f"ERROR: camera preflight conversion failed: {error}", file=sys.stderr)
            return 4

        mosaic_width = args.tile_width * 3
        mosaic_height = args.tile_height
        command = ffmpeg_command(
            executable=args.ffmpeg,
            output=output,
            width=mosaic_width,
            height=mosaic_height,
            fps=args.fps,
        )
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        if process.stdin is None:
            raise RuntimeError("ffmpeg stdin pipe is unavailable")
        atomic_json(
            ready,
            {
                "run_id": args.run_id,
                "pid": os.getpid(),
                "output": str(output),
                "topics": topics,
                "fps": args.fps,
                "mosaic_size": [mosaic_width, mosaic_height],
            },
        )
        print(f"READY {ready}", flush=True)
        terminal_reason = "recording"
        period = 1.0 / args.fps
        next_frame = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if now < next_frame:
                stop.wait(next_frame - now)
                continue
            snapshot = state.snapshot()
            if snapshot is not None:
                messages, source_ns = snapshot
                frames = {}
                conversion_failed = False
                for name, message in messages.items():
                    try:
                        converted = node.bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
                        frames[name] = np.asarray(converted, dtype=np.uint8)
                    except Exception as error:
                        state.conversion_failed(name)
                        node.get_logger().error(f"{name} conversion failed: {error}")
                        conversion_failed = True
                if conversion_failed:
                    next_frame += period
                    continue
                mosaic = compose_mosaic(
                    frames,
                    width=args.tile_width,
                    height=args.tile_height,
                    cv2=cv2,
                    np=np,
                    annotation=f"{args.run_id}  t={(time.monotonic_ns() - started_monotonic_ns) / 1_000_000_000:.1f}s",
                )
                process.stdin.write(mosaic.tobytes())
                frames_written += 1
                skew_ms = (max(source_ns.values()) - min(source_ns.values())) / 1_000_000
                max_source_skew_ms = max(max_source_skew_ms, skew_ms)
            if process.poll() is not None:
                raise RuntimeError(f"ffmpeg exited unexpectedly with code {process.returncode}")
            next_frame += period
            if next_frame < time.monotonic() - period:
                next_frame = time.monotonic()
        terminal_reason = "signal_stop"
        return 0
    except BrokenPipeError:
        terminal_reason = "ffmpeg_broken_pipe"
        print("ERROR: ffmpeg closed its input pipe", file=sys.stderr)
        return 5
    except Exception as error:
        terminal_reason = f"error:{type(error).__name__}"
        print(f"ERROR: {error}", file=sys.stderr)
        return 6
    finally:
        if ready.exists():
            ready.unlink()
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            try:
                ffmpeg_exit_code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    ffmpeg_exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    ffmpeg_exit_code = process.wait(timeout=5)
        if executor is not None:
            executor.shutdown(timeout_sec=2.0)
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        received, conversion_errors = state.counts()
        ended_monotonic_ns = time.monotonic_ns()
        manifest_payload = {
            "schema_version": 1,
            "run_id": args.run_id,
            "output": str(output),
            "topics": topics,
            "target_fps": args.fps,
            "tile_size": [args.tile_width, args.tile_height],
            "mosaic_size": [args.tile_width * 3, args.tile_height],
            "started_wall_unix_sec": started_wall,
            "duration_monotonic_sec": (ended_monotonic_ns - started_monotonic_ns) / 1_000_000_000,
            "frames_received": received,
            "conversion_errors": conversion_errors,
            "frames_written": frames_written,
            "max_source_timestamp_skew_ms": max_source_skew_ms,
            "ffmpeg_exit_code": ffmpeg_exit_code,
            "terminal_reason": terminal_reason,
            "output_exists": output.exists(),
            "output_bytes": output.stat().st_size if output.exists() else 0,
        }
        if not manifest.exists():
            atomic_json(manifest, manifest_payload)


if __name__ == "__main__":
    sys.exit(main())

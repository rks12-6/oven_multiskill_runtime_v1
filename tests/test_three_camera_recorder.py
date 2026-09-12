from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "record_three_cameras.py"
if not SCRIPT.exists():
    SCRIPT = HERE.parent / "tools" / "record_three_cameras.py"
SPEC = importlib.util.spec_from_file_location("record_three_cameras", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ThreeCameraRecorderTests(unittest.TestCase):
    def test_mosaic_places_front_camera_in_the_center(self) -> None:
        self.assertEqual(MODULE.MOSAIC_ORDER, ("left_wrist", "front", "right_wrist"))

    def test_ffmpeg_command_is_non_overwriting_and_h264(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mosaic.mp4"
            command = MODULE.ffmpeg_command(
                executable="ffmpeg", output=output, width=1920, height=480, fps=15.0
            )
            self.assertIn("-n", command)
            self.assertEqual(command[command.index("-c:v") + 1], "libx264")
            self.assertEqual(command[command.index("-pix_fmt", command.index("-i")) + 1], "yuv420p")
            self.assertEqual(command[-1], str(output))

    def test_output_paths_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mosaic.mp4"
            args = argparse.Namespace(output=output, manifest=None, ready_file=None)
            resolved_output, manifest, ready = MODULE.output_paths(args)
            self.assertEqual(resolved_output, output.resolve())
            self.assertEqual(manifest.name, "mosaic.recording.json")
            self.assertEqual(ready.name, "mosaic.ready.json")

    def test_output_paths_reject_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mosaic.mp4"
            args = argparse.Namespace(output=output, manifest=output, ready_file=None)
            with self.assertRaisesRegex(ValueError, "different paths"):
                MODULE.output_paths(args)

    def test_refuse_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "existing.mp4"
            existing.write_bytes(b"protected")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                MODULE.refuse_overwrite(existing)
            self.assertEqual(existing.read_bytes(), b"protected")

    def test_atomic_json_writes_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "manifest.json"
            MODULE.atomic_json(destination, {"run_id": "test", "frames_written": 12})
            text = destination.read_text(encoding="utf-8")
            self.assertIn('"run_id": "test"', text)
            self.assertIn('"frames_written": 12', text)


if __name__ == "__main__":
    unittest.main()

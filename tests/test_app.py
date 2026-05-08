from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


def fake_media(path: Path, *, has_video: bool = True, has_audio: bool = True) -> app.MediaInfo:
    return app.MediaInfo(
        path=path,
        duration=3.0,
        size_bytes=150_000,
        has_video=has_video,
        has_audio=has_audio,
        width=320 if has_video else None,
        height=240 if has_video else None,
        fps=24.0 if has_video else None,
        video_codec="h264" if has_video else None,
        audio_codec="aac" if has_audio else None,
    )


def make_options(input_path: Path, output_path: Path, **overrides: object) -> app.EncodeOptions:
    values = {
        "input_path": input_path,
        "output_path": output_path,
        "mode": "auto_size",
        "target_size_mb": 1.0,
        "include_audio": True,
        "audio_bitrate_kbps": None,
        "width": None,
        "height": None,
        "fps": None,
        "video_bitrate_kbps": None,
        "crf": None,
        "preset": "medium",
        "overwrite": True,
    }
    values.update(overrides)
    return app.EncodeOptions(**values)


class BackendValidationTests(unittest.TestCase):
    def test_rejects_non_positive_numeric_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "in.mp4"
            output_path = Path(temp_dir) / "out.mp4"
            input_path.write_bytes(b"input")
            media = fake_media(input_path)

            cases = [
                ("target_size_mb", 0, "Target size MB"),
                ("audio_bitrate_kbps", -1, "Audio kbps"),
                ("width", 0, "Width"),
                ("height", -1, "Height"),
                ("fps", 0.0, "FPS"),
                ("video_bitrate_kbps", -1, "Video kbps"),
                ("crf", 0, "CRF"),
            ]

            for field, value, label in cases:
                with self.subTest(field=field):
                    options = make_options(input_path, output_path, **{field: value})
                    with self.assertRaisesRegex(app.ConversionError, f"{label} must be greater than zero"):
                        app.validate_options(options, media)

    def test_no_overwrite_still_blocks_existing_final_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "in.mp4"
            output_path = Path(temp_dir) / "out.mp4"
            input_path.write_bytes(b"input")
            output_path.write_bytes(b"existing")
            media = fake_media(input_path)
            options = make_options(input_path, output_path, overwrite=False)

            with patch.object(app, "probe_media", return_value=media):
                with self.assertRaisesRegex(app.ConversionError, "Output file already exists"):
                    app.run_conversion(options)

    def test_auto_video_no_overwrite_uses_internal_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "in.mp4"
            output_path = Path(temp_dir) / "out.mp4"
            input_path.write_bytes(b"input")
            media = fake_media(input_path)
            options = make_options(input_path, output_path, target_size_mb=1.0, overwrite=False)

            def fake_run_streaming(command: list[str]) -> None:
                output = command[-1]
                if output != os.devnull:
                    Path(output).write_bytes(b"x" * 970_000)

            with patch.object(app, "run_streaming", side_effect=fake_run_streaming):
                with patch.object(app, "probe_media", return_value=media):
                    result = app.run_auto_video(options, media)

            self.assertEqual(1, result.attempts)
            self.assertEqual("-y", result.commands[0][1])
            self.assertEqual("-y", result.commands[1][1])
            self.assertNotIn("-n", result.commands[0])
            self.assertNotIn("-n", result.commands[1])

    def test_auto_audio_retries_overwrite_prior_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "in.mp4"
            output_path = Path(temp_dir) / "out.mp3"
            input_path.write_bytes(b"input")
            media = fake_media(input_path)
            options = make_options(input_path, output_path, target_size_mb=0.02, overwrite=False)
            sizes = [18_000, 19_500]

            def fake_run_streaming(command: list[str]) -> None:
                output_path.write_bytes(b"x" * sizes.pop(0))

            with patch.object(app, "run_streaming", side_effect=fake_run_streaming):
                with patch.object(app, "probe_media", return_value=media):
                    result = app.run_auto_audio(options, media)

            self.assertEqual(2, result.attempts)
            self.assertTrue(all(command[1] == "-y" for command in result.commands))


if __name__ == "__main__":
    unittest.main()

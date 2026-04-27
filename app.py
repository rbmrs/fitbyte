#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


VIDEO_OUTPUT_EXTS = {".mp4", ".mkv", ".mov"}
AUDIO_OUTPUT_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
SUPPORTED_INPUT_EXTS = VIDEO_OUTPUT_EXTS | AUDIO_OUTPUT_EXTS | {
    ".webm",
    ".avi",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".wmv",
    ".flv",
}
PRESET_OPTIONS = ["medium", "slow", "veryslow"]
MODE_OPTIONS = ["auto_size", "manual"]
BOOL_OPTIONS = ["yes", "no"]
LOSSLESS_AUDIO_CODECS = {"pcm_s16le", "flac"}
AUTO_SIZE_AUDIO_UNSUPPORTED_EXTS = {".wav", ".flac"}


class ConversionError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    path: Path
    duration: float
    size_bytes: int
    has_video: bool
    has_audio: bool
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]
    video_codec: Optional[str]
    audio_codec: Optional[str]


@dataclass
class EncodeOptions:
    input_path: Path
    output_path: Path
    mode: str
    target_size_mb: Optional[float]
    include_audio: bool
    audio_bitrate_kbps: Optional[int]
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]
    video_bitrate_kbps: Optional[int]
    crf: Optional[int]
    preset: str
    overwrite: bool = True


@dataclass
class ConversionResult:
    output_path: Path
    size_bytes: int
    duration: float
    attempts: int
    mode: str
    commands: List[List[str]]


@dataclass
class FieldSpec:
    key: str
    label: str
    kind: str
    options: Optional[Sequence[str]] = None


FORM_FIELDS = [
    FieldSpec("input_path", "Input path", "text"),
    FieldSpec("output_path", "Output path", "text"),
    FieldSpec("mode", "Mode", "cycle", MODE_OPTIONS),
    FieldSpec("target_size_mb", "Target size MB", "text"),
    FieldSpec("include_audio", "Keep audio", "cycle", BOOL_OPTIONS),
    FieldSpec("audio_bitrate_kbps", "Audio kbps", "text"),
    FieldSpec("width", "Width", "text"),
    FieldSpec("height", "Height", "text"),
    FieldSpec("fps", "FPS", "text"),
    FieldSpec("video_bitrate_kbps", "Video kbps", "text"),
    FieldSpec("crf", "CRF", "text"),
    FieldSpec("preset", "Preset", "cycle", PRESET_OPTIONS),
]


def human_size(size_bytes: int) -> str:
    if size_bytes < 1_000:
        return f"{size_bytes} B"
    units = ["KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        size /= 1_000.0
        if size < 1_000 or unit == units[-1]:
            return f"{size:.2f} {unit}"
    return f"{size_bytes} B"


def parse_frame_rate(value: Optional[str]) -> Optional[float]:
    if not value or value == "0/0":
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            numerator_value = float(numerator)
            denominator_value = float(denominator)
        except ValueError:
            return None
        if denominator_value == 0:
            return None
        return numerator_value / denominator_value
    try:
        return float(value)
    except ValueError:
        return None


def run_capture(cmd: Sequence[str]) -> str:
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip() or shlex.join(cmd)
        raise ConversionError(stderr)
    return proc.stdout


def run_streaming(cmd: Sequence[str]) -> None:
    proc = subprocess.run(list(cmd), check=False)
    if proc.returncode != 0:
        raise ConversionError(f"Command failed with exit code {proc.returncode}: {shlex.join(cmd)}")


def probe_media(path: Path) -> MediaInfo:
    if not path.exists():
        raise ConversionError(f"Input file does not exist: {path}")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate",
        str(path),
    ]
    raw = run_capture(cmd)
    payload = json.loads(raw)
    streams = payload.get("streams", [])
    format_info = payload.get("format", {})
    duration = float(format_info.get("duration") or 0.0)
    size_bytes = int(float(format_info.get("size") or path.stat().st_size))

    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)

    return MediaInfo(
        path=path,
        duration=duration,
        size_bytes=size_bytes,
        has_video=video_stream is not None,
        has_audio=audio_stream is not None,
        width=video_stream.get("width") if video_stream else None,
        height=video_stream.get("height") if video_stream else None,
        fps=parse_frame_rate(video_stream.get("r_frame_rate") if video_stream else None),
        video_codec=video_stream.get("codec_name") if video_stream else None,
        audio_codec=audio_stream.get("codec_name") if audio_stream else None,
    )


def find_single_media_file(base_dir: Path) -> Optional[Path]:
    candidates = [
        path
        for path in sorted(base_dir.iterdir())
        if path.is_file() and path.suffix.lower() in SUPPORTED_INPUT_EXTS
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def output_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_OUTPUT_EXTS:
        return "video"
    if ext in AUDIO_OUTPUT_EXTS:
        return "audio"
    raise ConversionError(
        f"Unsupported output extension '{ext or '(none)'}'. Supported video: {sorted(VIDEO_OUTPUT_EXTS)}. "
        f"Supported audio: {sorted(AUDIO_OUTPUT_EXTS)}."
    )


def default_output_extension(media: Optional[MediaInfo], input_path: Path) -> str:
    if media and media.has_video:
        return ".mp4"
    if media and media.has_audio:
        return ".mp3"
    ext = input_path.suffix.lower()
    if ext in VIDEO_OUTPUT_EXTS:
        return ".mp4"
    return ".mp3"


def suggest_output_path(input_path: Path, media: Optional[MediaInfo]) -> Path:
    return input_path.with_name(f"{input_path.stem}_converted{default_output_extension(media, input_path)}")


def parse_optional_int(raw_value: str, label: str) -> Optional[int]:
    value = raw_value.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConversionError(f"{label} must be an integer.") from exc
    if parsed <= 0:
        raise ConversionError(f"{label} must be greater than zero.")
    return parsed


def parse_optional_float(raw_value: str, label: str) -> Optional[float]:
    value = raw_value.strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConversionError(f"{label} must be a number.") from exc
    if parsed <= 0:
        raise ConversionError(f"{label} must be greater than zero.")
    return parsed


def video_codec_for_ext(ext: str) -> str:
    if ext in VIDEO_OUTPUT_EXTS:
        return "libx264"
    raise ConversionError(f"Unsupported video output extension: {ext}")


def audio_codec_for_ext(ext: str) -> str:
    mapping = {
        ".mp4": "aac",
        ".mkv": "aac",
        ".mov": "aac",
        ".mp3": "libmp3lame",
        ".m4a": "aac",
        ".aac": "aac",
        ".wav": "pcm_s16le",
        ".flac": "flac",
        ".ogg": "libvorbis",
        ".opus": "libopus",
    }
    try:
        return mapping[ext]
    except KeyError as exc:
        raise ConversionError(f"Unsupported audio output extension: {ext}") from exc


def default_audio_bitrate_kbps(ext: str) -> Optional[int]:
    codec = audio_codec_for_ext(ext)
    if codec in LOSSLESS_AUDIO_CODECS:
        return None
    if ext == ".opus":
        return 96
    return 128


def build_filter_chain(options: EncodeOptions) -> List[str]:
    filters: List[str] = []
    if options.width and options.height:
        filters.append(f"scale={options.width}:{options.height}")
    elif options.width:
        filters.append(f"scale={options.width}:-2")
    elif options.height:
        filters.append(f"scale=-2:{options.height}")
    if options.fps:
        fps_text = f"{options.fps:.3f}".rstrip("0").rstrip(".")
        filters.append(f"fps={fps_text}")
    return filters


def validate_options(options: EncodeOptions, media: MediaInfo) -> None:
    if not options.input_path.exists():
        raise ConversionError(f"Input file does not exist: {options.input_path}")
    if options.input_path.resolve() == options.output_path.resolve():
        raise ConversionError("Input and output paths must be different.")
    out_kind = output_kind(options.output_path)
    ext = options.output_path.suffix.lower()

    if out_kind == "video" and not media.has_video:
        raise ConversionError("The input file has no video stream, so it cannot be converted to a video container.")
    if out_kind == "audio" and not media.has_audio:
        raise ConversionError("The input file has no audio stream, so it cannot be converted to an audio-only file.")
    if out_kind == "audio" and not options.include_audio:
        raise ConversionError("Audio-only outputs require audio to be enabled.")

    if options.mode == "auto_size":
        if not options.target_size_mb:
            raise ConversionError("Target size MB is required in auto-size mode.")
        if options.target_size_mb <= 0:
            raise ConversionError("Target size MB must be greater than zero.")
        if media.duration <= 0:
            raise ConversionError("The input duration is zero, so auto-size mode cannot compute a bitrate budget.")
        if out_kind == "audio" and ext in AUTO_SIZE_AUDIO_UNSUPPORTED_EXTS:
            raise ConversionError(
                f"Auto-size mode does not support {ext} because the codec is lossless. Use a bitrate-driven format like .mp3, .m4a, .ogg, or .opus."
            )
    elif options.mode != "manual":
        raise ConversionError(f"Unsupported mode: {options.mode}")

    if options.video_bitrate_kbps and options.crf is not None and options.mode == "manual":
        options.crf = None

    if options.mode == "manual" and out_kind == "video":
        if options.video_bitrate_kbps is None and options.crf is None:
            options.crf = 23

    if out_kind == "video" and options.include_audio and media.has_audio and options.audio_bitrate_kbps is None:
        options.audio_bitrate_kbps = default_audio_bitrate_kbps(ext)

    if out_kind == "audio" and options.audio_bitrate_kbps is None:
        options.audio_bitrate_kbps = default_audio_bitrate_kbps(ext)

    if options.preset not in PRESET_OPTIONS:
        raise ConversionError(f"Preset must be one of {', '.join(PRESET_OPTIONS)}.")


def passlog_prefix(base_dir: str, attempt: int) -> str:
    return os.path.join(base_dir, f"ffmpeg2pass-{attempt}")


def build_video_encode_command(
    options: EncodeOptions,
    media: MediaInfo,
    video_kbps: Optional[int],
    audio_kbps: Optional[int],
    pass_number: Optional[int] = None,
    passlogfile: Optional[str] = None,
    output_override: Optional[str] = None,
) -> List[str]:
    ext = options.output_path.suffix.lower()
    cmd = [
        "ffmpeg",
        "-y" if options.overwrite else "-n",
        "-i",
        str(options.input_path),
        "-map",
        "0:v:0",
    ]
    if options.include_audio and media.has_audio and pass_number != 1:
        cmd.extend(["-map", "0:a:0"])

    filter_chain = build_filter_chain(options)
    if filter_chain:
        cmd.extend(["-vf", ",".join(filter_chain)])

    cmd.extend(["-c:v", video_codec_for_ext(ext), "-preset", options.preset])
    if video_kbps is not None:
        cmd.extend(["-b:v", f"{video_kbps}k"])
    elif options.video_bitrate_kbps is not None:
        cmd.extend(["-b:v", f"{options.video_bitrate_kbps}k"])
    else:
        cmd.extend(["-crf", str(options.crf if options.crf is not None else 23)])

    if pass_number is not None:
        if not passlogfile:
            raise ConversionError("Two-pass encode requested without a passlog file.")
        cmd.extend(["-pass", str(pass_number), "-passlogfile", passlogfile])

    if pass_number == 1:
        container = "matroska" if ext == ".mkv" else ext.lstrip(".")
        cmd.extend(["-an", "-f", container, output_override or os.devnull])
        return cmd

    if options.include_audio and media.has_audio:
        audio_codec = audio_codec_for_ext(ext)
        cmd.extend(["-c:a", audio_codec])
        if audio_kbps and audio_codec not in LOSSLESS_AUDIO_CODECS:
            cmd.extend(["-b:a", f"{audio_kbps}k"])
    else:
        cmd.append("-an")

    if ext in {".mp4", ".mov"}:
        cmd.extend(["-movflags", "+faststart"])

    cmd.append(output_override or str(options.output_path))
    return cmd


def build_audio_encode_command(
    options: EncodeOptions,
    audio_kbps: Optional[int],
    output_override: Optional[str] = None,
) -> List[str]:
    ext = options.output_path.suffix.lower()
    codec = audio_codec_for_ext(ext)
    cmd = [
        "ffmpeg",
        "-y" if options.overwrite else "-n",
        "-i",
        str(options.input_path),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        codec,
    ]
    if audio_kbps and codec not in LOSSLESS_AUDIO_CODECS:
        cmd.extend(["-b:a", f"{audio_kbps}k"])
    if ext == ".m4a":
        cmd.extend(["-movflags", "+faststart"])
    cmd.append(output_override or str(options.output_path))
    return cmd


def initial_auto_budgets(options: EncodeOptions, media: MediaInfo) -> Tuple[Optional[int], Optional[int]]:
    target_bytes = int(options.target_size_mb * 1_000_000)
    usable_bytes = int(target_bytes * 0.985)
    total_kbps = max(8.0, usable_bytes * 8.0 / media.duration / 1000.0)
    ext = options.output_path.suffix.lower()
    kind = output_kind(options.output_path)

    if kind == "audio":
        return None, max(24, int(total_kbps))

    audio_kbps = 0
    if options.include_audio and media.has_audio:
        audio_kbps = options.audio_bitrate_kbps or default_audio_bitrate_kbps(ext) or 0
    video_kbps = int(total_kbps - audio_kbps)
    if video_kbps < 80:
        raise ConversionError(
            f"Target size is too small for the current duration/settings. Computed video budget is only {video_kbps} kbps."
        )
    return video_kbps, audio_kbps or None


def scale_bitrate(current_kbps: int, target_bytes: int, actual_bytes: int, reduce: bool) -> int:
    ratio = float(target_bytes) / float(actual_bytes)
    if reduce:
        return max(48, int(math.floor(current_kbps * ratio * 0.985)))
    growth = min(ratio * 0.995, 1.08)
    next_value = int(math.floor(current_kbps * growth))
    return max(current_kbps + 1, next_value)


def run_auto_video(options: EncodeOptions, media: MediaInfo) -> ConversionResult:
    target_bytes = int(options.target_size_mb * 1_000_000)
    video_kbps, audio_kbps = initial_auto_budgets(options, media)
    commands: List[List[str]] = []
    attempts = 0

    with tempfile.TemporaryDirectory(prefix="media-convert-pass-") as temp_dir:
        for attempt in range(1, 6):
            attempts = attempt
            passlog = passlog_prefix(temp_dir, attempt)
            cmd1 = build_video_encode_command(
                options,
                media,
                video_kbps=video_kbps,
                audio_kbps=audio_kbps,
                pass_number=1,
                passlogfile=passlog,
                output_override=os.devnull,
            )
            cmd2 = build_video_encode_command(
                options,
                media,
                video_kbps=video_kbps,
                audio_kbps=audio_kbps,
                pass_number=2,
                passlogfile=passlog,
            )
            commands.extend([cmd1, cmd2])
            run_streaming(cmd1)
            run_streaming(cmd2)

            actual_bytes = options.output_path.stat().st_size
            if actual_bytes <= target_bytes and actual_bytes >= int(target_bytes * 0.965):
                break
            if actual_bytes == 0:
                raise ConversionError("ffmpeg produced an empty file.")
            if actual_bytes <= target_bytes and attempt >= 3:
                break
            video_kbps = scale_bitrate(video_kbps, target_bytes, actual_bytes, reduce=actual_bytes > target_bytes)
        final_bytes = options.output_path.stat().st_size

    if final_bytes > target_bytes:
        raise ConversionError(
            f"Auto-size mode could not get under the target. Final size: {final_bytes} bytes, target: {target_bytes} bytes."
        )

    final_media = probe_media(options.output_path)
    return ConversionResult(
        output_path=options.output_path,
        size_bytes=final_bytes,
        duration=final_media.duration,
        attempts=attempts,
        mode=options.mode,
        commands=commands,
    )


def run_auto_audio(options: EncodeOptions, media: MediaInfo) -> ConversionResult:
    target_bytes = int(options.target_size_mb * 1_000_000)
    _, audio_kbps = initial_auto_budgets(options, media)
    commands: List[List[str]] = []
    attempts = 0

    for attempt in range(1, 6):
        attempts = attempt
        cmd = build_audio_encode_command(options, audio_kbps=audio_kbps)
        commands.append(cmd)
        run_streaming(cmd)
        actual_bytes = options.output_path.stat().st_size
        if actual_bytes <= target_bytes and actual_bytes >= int(target_bytes * 0.965):
            break
        if actual_bytes <= target_bytes and attempt >= 3:
            break
        if actual_bytes == 0:
            raise ConversionError("ffmpeg produced an empty file.")
        audio_kbps = scale_bitrate(audio_kbps or 64, target_bytes, actual_bytes, reduce=actual_bytes > target_bytes)

    final_bytes = options.output_path.stat().st_size
    if final_bytes > target_bytes:
        raise ConversionError(
            f"Auto-size mode could not get under the target. Final size: {final_bytes} bytes, target: {target_bytes} bytes."
        )
    final_media = probe_media(options.output_path)
    return ConversionResult(
        output_path=options.output_path,
        size_bytes=final_bytes,
        duration=final_media.duration,
        attempts=attempts,
        mode=options.mode,
        commands=commands,
    )


def run_manual(options: EncodeOptions, media: MediaInfo) -> ConversionResult:
    kind = output_kind(options.output_path)
    if kind == "video":
        cmd = build_video_encode_command(options, media, video_kbps=None, audio_kbps=options.audio_bitrate_kbps)
    else:
        cmd = build_audio_encode_command(options, audio_kbps=options.audio_bitrate_kbps)
    run_streaming(cmd)
    final_media = probe_media(options.output_path)
    return ConversionResult(
        output_path=options.output_path,
        size_bytes=options.output_path.stat().st_size,
        duration=final_media.duration,
        attempts=1,
        mode=options.mode,
        commands=[cmd],
    )


def run_conversion(options: EncodeOptions) -> ConversionResult:
    media = probe_media(options.input_path)
    validate_options(options, media)
    if options.output_path.exists() and not options.overwrite:
        raise ConversionError(f"Output file already exists: {options.output_path}")
    kind = output_kind(options.output_path)
    if options.mode == "manual":
        return run_manual(options, media)
    if kind == "video":
        return run_auto_video(options, media)
    return run_auto_audio(options, media)


def preview_commands(options: EncodeOptions, media: MediaInfo) -> List[str]:
    validate_options(options, media)
    kind = output_kind(options.output_path)
    if options.mode == "manual":
        if kind == "video":
            cmd = build_video_encode_command(options, media, None, options.audio_bitrate_kbps)
        else:
            cmd = build_audio_encode_command(options, options.audio_bitrate_kbps)
        return [shlex.join(cmd)]

    video_kbps, audio_kbps = initial_auto_budgets(options, media)
    if kind == "video":
        preview_passlog = "/tmp/media-convert-preview"
        cmd1 = build_video_encode_command(
            options,
            media,
            video_kbps=video_kbps,
            audio_kbps=audio_kbps,
            pass_number=1,
            passlogfile=preview_passlog,
            output_override=os.devnull,
        )
        cmd2 = build_video_encode_command(
            options,
            media,
            video_kbps=video_kbps,
            audio_kbps=audio_kbps,
            pass_number=2,
            passlogfile=preview_passlog,
        )
        return [
            f"initial video budget: {video_kbps} kbps",
            shlex.join(cmd1),
            shlex.join(cmd2),
            "auto-size may rerun with adjusted bitrate if the first attempt misses the target",
        ]
    cmd = build_audio_encode_command(options, audio_kbps=audio_kbps)
    return [
        f"initial audio budget: {audio_kbps} kbps",
        shlex.join(cmd),
        "auto-size may rerun with adjusted bitrate if the first attempt misses the target",
    ]


def print_result(result: ConversionResult) -> None:
    print()
    print("Conversion complete")
    print(f"Output: {result.output_path}")
    print(f"Size:   {human_size(result.size_bytes)} ({result.size_bytes} bytes)")
    print(f"Length: {result.duration:.3f} seconds")
    print(f"Mode:   {result.mode}")
    print(f"Runs:   {result.attempts}")


def wrap_lines(lines: Sequence[str], width: int) -> List[str]:
    wrapped: List[str] = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, width=width, replace_whitespace=False) or [""])
    return wrapped


class TuiState:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.mode = "auto_size"
        self.target_size_mb = "10"
        self.include_audio = "yes"
        self.audio_bitrate_kbps = "96"
        self.width = ""
        self.height = ""
        self.fps = ""
        self.video_bitrate_kbps = ""
        self.crf = "23"
        self.preset = "slow"
        self.message = "Enter edits with Enter. Cycle options with Left/Right or Space. Convert with C."

        candidate = find_single_media_file(cwd)
        self.input_path = str(candidate) if candidate else ""
        self.output_path = ""
        self._last_suggested_output = ""
        self._probe_cache_path = ""
        self._probe_cache_media: Optional[MediaInfo] = None
        self._probe_cache_error = ""
        self._refresh_output_suggestion(force=True)

    def get_value(self, key: str) -> str:
        return getattr(self, key)

    def set_value(self, key: str, value: str) -> None:
        setattr(self, key, value)
        if key == "input_path":
            self._probe_cache_path = ""
            self._probe_cache_media = None
            self._probe_cache_error = ""
            self._refresh_output_suggestion(force=False)
        elif key == "output_path":
            self._last_suggested_output = value

    def cycle(self, key: str, delta: int) -> None:
        spec = next(field for field in FORM_FIELDS if field.key == key)
        if not spec.options:
            return
        current = self.get_value(key)
        values = list(spec.options)
        index = values.index(current)
        next_index = (index + delta) % len(values)
        self.set_value(key, values[next_index])

    def _refresh_output_suggestion(self, force: bool) -> None:
        raw_input = self.input_path.strip()
        if not raw_input:
            return
        input_path = Path(raw_input).expanduser()
        suggested = str(suggest_output_path(input_path, self.get_probe_media()))
        if force or not self.output_path or self.output_path == self._last_suggested_output:
            self.output_path = suggested
            self._last_suggested_output = suggested

    def get_probe_media(self) -> Optional[MediaInfo]:
        raw_input = self.input_path.strip()
        if not raw_input:
            return None
        input_path = str(Path(raw_input).expanduser())
        if self._probe_cache_path == input_path:
            return self._probe_cache_media
        self._probe_cache_path = input_path
        self._probe_cache_media = None
        self._probe_cache_error = ""
        try:
            media = probe_media(Path(input_path))
        except Exception as exc:
            self._probe_cache_error = str(exc)
            return None
        self._probe_cache_media = media
        return media

    def get_probe_error(self) -> str:
        self.get_probe_media()
        return self._probe_cache_error

    def media_summary(self) -> List[str]:
        media = self.get_probe_media()
        if media is None:
            if self.get_probe_error():
                return [f"Probe error: {self.get_probe_error()}"]
            return ["No input selected."]
        lines = [
            f"size: {human_size(media.size_bytes)} ({media.size_bytes} bytes)",
            f"duration: {media.duration:.3f}s",
            f"video: {'yes' if media.has_video else 'no'}",
            f"audio: {'yes' if media.has_audio else 'no'}",
        ]
        if media.has_video:
            fps_text = f"{media.fps:.2f}" if media.fps else "unknown"
            lines.append(f"video stream: {media.video_codec or 'unknown'} {media.width}x{media.height} @ {fps_text} fps")
        if media.has_audio:
            lines.append(f"audio stream: {media.audio_codec or 'unknown'}")
        return lines

    def build_options(self) -> EncodeOptions:
        raw_input = self.input_path.strip()
        raw_output = self.output_path.strip()
        if not raw_input:
            raise ConversionError("Input path is required.")
        if not raw_output:
            raise ConversionError("Output path is required.")
        options = EncodeOptions(
            input_path=Path(raw_input).expanduser(),
            output_path=Path(raw_output).expanduser(),
            mode=self.mode,
            target_size_mb=parse_optional_float(self.target_size_mb, "Target size MB"),
            include_audio=self.include_audio == "yes",
            audio_bitrate_kbps=parse_optional_int(self.audio_bitrate_kbps, "Audio kbps"),
            width=parse_optional_int(self.width, "Width"),
            height=parse_optional_int(self.height, "Height"),
            fps=parse_optional_float(self.fps, "FPS"),
            video_bitrate_kbps=parse_optional_int(self.video_bitrate_kbps, "Video kbps"),
            crf=parse_optional_int(self.crf, "CRF"),
            preset=self.preset,
            overwrite=True,
        )
        media = self.get_probe_media()
        if media is None:
            error = self.get_probe_error() or "Unable to probe input."
            raise ConversionError(error)
        validate_options(options, media)
        return options

    def preview_lines(self, width: int) -> List[str]:
        media = self.get_probe_media()
        if media is None:
            return wrap_lines(self.media_summary(), width)
        try:
            options = self.build_options()
            lines = preview_commands(options, media)
        except Exception as exc:
            lines = [f"Preview unavailable: {exc}"]
        return wrap_lines(lines, width)


def prompt_for_value(stdscr: "curses._CursesWindow", label: str, current_value: str) -> Optional[str]:
    height, width = stdscr.getmaxyx()
    prompt = f"{label} (blank clears, Esc cancels). Current: {current_value or '<empty>'}"
    stdscr.move(height - 2, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(height - 2, 0, prompt, width - 1, curses.A_BOLD)
    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    curses.echo()
    curses.curs_set(1)
    try:
        raw = stdscr.getstr(height - 1, 0, width - 1)
    except KeyboardInterrupt:
        raw = b""
    finally:
        curses.noecho()
        curses.curs_set(0)
    if raw == b"\x1b":
        return None
    return raw.decode("utf-8").strip()


def draw_tui(stdscr: "curses._CursesWindow", state: TuiState, selected_index: int) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    left_width = max(42, width // 2)
    right_x = min(left_width + 2, width - 1)

    stdscr.addnstr(0, 0, "Media Convert TUI", width - 1, curses.A_BOLD)
    help_line = "Arrows/Tab move | Enter edits | Left/Right/Space cycle | C convert | Q quit"
    stdscr.addnstr(1, 0, help_line, width - 1)

    for index, field in enumerate(FORM_FIELDS):
        y = 3 + index
        if y >= height - 4:
            break
        label = f"{field.label:>14}: "
        value = state.get_value(field.key) or "<empty>"
        attr = curses.A_REVERSE if index == selected_index else curses.A_NORMAL
        stdscr.addnstr(y, 0, label, left_width - 1, attr)
        stdscr.addnstr(y, len(label), value, left_width - len(label) - 1, attr)

    summary_y = 3
    stdscr.addnstr(summary_y - 1, right_x, "Input summary", width - right_x - 1, curses.A_BOLD)
    for offset, line in enumerate(wrap_lines(state.media_summary(), width - right_x - 1)[:8]):
        stdscr.addnstr(summary_y + offset, right_x, line, width - right_x - 1)

    preview_y = summary_y + 9
    stdscr.addnstr(preview_y - 1, right_x, "Command preview", width - right_x - 1, curses.A_BOLD)
    preview_lines = state.preview_lines(max(20, width - right_x - 1))
    for offset, line in enumerate(preview_lines[: max(4, height - preview_y - 3)]):
        stdscr.addnstr(preview_y + offset, right_x, line, width - right_x - 1)

    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(height - 1, 0, state.message, width - 1, curses.A_DIM)
    stdscr.refresh()


def run_tui(stdscr: "curses._CursesWindow") -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    state = TuiState(Path.cwd())
    selected_index = 0

    while True:
        draw_tui(stdscr, state, selected_index)
        key = stdscr.getch()

        if key in (ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            selected_index = (selected_index - 1) % len(FORM_FIELDS)
            continue
        if key in (curses.KEY_DOWN, ord("j"), 9):
            selected_index = (selected_index + 1) % len(FORM_FIELDS)
            continue
        current = FORM_FIELDS[selected_index]

        if key in (curses.KEY_LEFT,):
            if current.kind == "cycle":
                state.cycle(current.key, -1)
            continue
        if key in (curses.KEY_RIGHT, ord(" ")):
            if current.kind == "cycle":
                state.cycle(current.key, 1)
            continue
        if key in (10, 13, curses.KEY_ENTER):
            if current.kind == "cycle":
                state.cycle(current.key, 1)
                continue
            new_value = prompt_for_value(stdscr, current.label, state.get_value(current.key))
            if new_value is not None:
                state.set_value(current.key, new_value)
                state.message = f"Updated {current.label}."
            continue
        if key in (ord("c"), ord("C")):
            try:
                options = state.build_options()
            except Exception as exc:
                state.message = str(exc)
                continue

            curses.def_prog_mode()
            curses.endwin()
            try:
                result = run_conversion(options)
                print_result(result)
                input("\nPress Enter to return to the TUI...")
                state.message = f"Finished: {result.output_path.name} ({result.size_bytes} bytes)"
            except Exception as exc:
                print(f"\nConversion failed: {exc}")
                input("\nPress Enter to return to the TUI...")
                state.message = str(exc)
            finally:
                curses.reset_prog_mode()
                curses.curs_set(0)
                stdscr.refresh()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Terminal media converter with manual controls and target-size automation."
    )
    parser.add_argument("--tui", action="store_true", help="Launch the curses TUI.")
    parser.add_argument("--input", type=Path, help="Input media file.")
    parser.add_argument("--output", type=Path, help="Output media file.")
    parser.add_argument("--mode", choices=MODE_OPTIONS, default="auto_size")
    parser.add_argument("--target-size-mb", type=float, default=10.0)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--video-bitrate-kbps", type=int)
    parser.add_argument("--audio-bitrate-kbps", type=int)
    parser.add_argument("--crf", type=int, default=23)
    parser.add_argument("--preset", choices=PRESET_OPTIONS, default="slow")
    parser.add_argument("--no-audio", action="store_true", help="Strip audio from the output.")
    parser.add_argument("--dry-run", action="store_true", help="Print the initial ffmpeg command(s) without converting.")
    return parser


def options_from_args(args: argparse.Namespace) -> EncodeOptions:
    if not args.input:
        raise ConversionError("--input is required in non-interactive mode.")
    if not args.output:
        raise ConversionError("--output is required in non-interactive mode.")
    return EncodeOptions(
        input_path=args.input.expanduser(),
        output_path=args.output.expanduser(),
        mode=args.mode,
        target_size_mb=args.target_size_mb if args.mode == "auto_size" else None,
        include_audio=not args.no_audio,
        audio_bitrate_kbps=args.audio_bitrate_kbps,
        width=args.width,
        height=args.height,
        fps=args.fps,
        video_bitrate_kbps=args.video_bitrate_kbps,
        crf=None if args.video_bitrate_kbps else args.crf,
        preset=args.preset,
        overwrite=True,
    )


def run_cli(args: argparse.Namespace) -> int:
    options = options_from_args(args)
    media = probe_media(options.input_path)
    validate_options(options, media)
    if args.dry_run:
        for line in preview_commands(options, media):
            print(line)
        return 0
    result = run_conversion(options)
    print_result(result)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(effective_argv)
    wants_tui = args.tui or len(effective_argv) == 0
    try:
        if wants_tui:
            curses.wrapper(run_tui)
            return 0
        return run_cli(args)
    except ConversionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCanceled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple


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


def state_file_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "shrinky" / "state.json"


def load_persisted_state() -> dict:
    path = state_file_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_persisted_state(data: dict) -> None:
    path = state_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def ensure_dependencies() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise ConversionError(
            f"Required tool(s) not found on PATH: {', '.join(missing)}. Install ffmpeg (which bundles ffprobe)."
        )


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


@dataclass
class ProgressUpdate:
    phase: str = ""
    fraction: float = 0.0
    speed: str = ""
    out_time_seconds: float = 0.0
    extra: str = ""


@dataclass
class ProgressContext:
    log_path: Path
    callback: Optional[Callable[[ProgressUpdate], None]] = None
    cancel_event: Optional[threading.Event] = None
    phase: str = ""
    phase_weight: float = 1.0
    phase_start: float = 0.0
    duration: float = 0.0
    log_lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, update: ProgressUpdate) -> None:
        if self.callback is not None:
            self.callback(update)

    def append_log(self, text: str) -> None:
        if not text:
            return
        with self.log_lock:
            try:
                with self.log_path.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(text)
                    if not text.endswith("\n"):
                        fh.write("\n")
            except OSError:
                pass


PROGRESS_KV = re.compile(r"^([a-zA-Z_]+)=(.*)$")


def _inject_progress_flags(cmd: Sequence[str]) -> List[str]:
    new_cmd = list(cmd)
    if new_cmd and new_cmd[0].endswith("ffmpeg"):
        injected = [new_cmd[0], "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
        injected.extend(new_cmd[1:])
        return injected
    return new_cmd


def run_streaming_with_progress(cmd: Sequence[str], ctx: ProgressContext) -> None:
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise ConversionError("Canceled by user.")

    cmd_list = _inject_progress_flags(cmd)
    ctx.append_log(f"\n$ {shlex.join(cmd_list)}")

    proc = subprocess.Popen(
        cmd_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(proc.stderr, ctx),
        daemon=True,
    )
    stderr_thread.start()

    watchdog_stop = threading.Event()

    def watchdog() -> None:
        while not watchdog_stop.is_set():
            if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                return
            if watchdog_stop.wait(0.1):
                return

    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    watchdog_thread.start()

    current = ProgressUpdate(phase=ctx.phase)
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            match = PROGRESS_KV.match(line)
            if not match:
                continue
            key, value = match.group(1), match.group(2).strip()
            if key == "out_time_ms" or key == "out_time_us":
                try:
                    micros = float(value)
                    current.out_time_seconds = micros / 1_000_000.0
                except ValueError:
                    pass
            elif key == "out_time":
                current.out_time_seconds = _parse_ffmpeg_time(value)
            elif key == "speed":
                current.speed = value
            elif key == "progress":
                if ctx.duration > 0:
                    fraction = current.out_time_seconds / ctx.duration
                    fraction = max(0.0, min(1.0, fraction))
                else:
                    fraction = 0.0 if value != "end" else 1.0
                if value == "end":
                    fraction = 1.0
                current.fraction = ctx.phase_start + fraction * ctx.phase_weight
                current.phase = ctx.phase
                ctx.emit(current)
                current = ProgressUpdate(phase=ctx.phase, fraction=current.fraction)
    finally:
        if proc.stdout:
            proc.stdout.close()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
        watchdog_stop.set()
        watchdog_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)

    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise ConversionError("Canceled by user.")
    if proc.returncode != 0:
        raise ConversionError(f"ffmpeg failed (exit {proc.returncode}). See log: {ctx.log_path}")


def _drain_stream(stream, ctx: ProgressContext) -> None:
    try:
        for line in stream:
            ctx.append_log(line.rstrip("\n"))
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _parse_ffmpeg_time(value: str) -> float:
    try:
        parts = value.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def create_log_file() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")) / "shrinky" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return base / f"convert-{stamp}.log"


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

    if options.mode == "manual" and out_kind == "video":
        if options.video_bitrate_kbps is not None and options.crf is not None:
            raise ConversionError(
                "Manual mode accepts either video bitrate or CRF, not both. Clear one of them."
            )
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


def run_auto_video(options: EncodeOptions, media: MediaInfo, ctx: Optional[ProgressContext] = None) -> ConversionResult:
    target_bytes = int(options.target_size_mb * 1_000_000)
    video_kbps, audio_kbps = initial_auto_budgets(options, media)
    commands: List[List[str]] = []
    attempts = 0

    max_attempts = 5
    if ctx is not None:
        ctx.duration = media.duration

    with tempfile.TemporaryDirectory(prefix="shrinky-pass-") as temp_dir:
        for attempt in range(1, max_attempts + 1):
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
            if ctx is not None:
                attempt_base = (attempt - 1) / max_attempts
                attempt_span = 1.0 / max_attempts
                ctx.phase = f"attempt {attempt}/{max_attempts} pass 1"
                ctx.phase_start = attempt_base
                ctx.phase_weight = attempt_span * 0.5
                run_streaming_with_progress(cmd1, ctx)
                ctx.phase = f"attempt {attempt}/{max_attempts} pass 2"
                ctx.phase_start = attempt_base + attempt_span * 0.5
                ctx.phase_weight = attempt_span * 0.5
                run_streaming_with_progress(cmd2, ctx)
            else:
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


def run_auto_audio(options: EncodeOptions, media: MediaInfo, ctx: Optional[ProgressContext] = None) -> ConversionResult:
    target_bytes = int(options.target_size_mb * 1_000_000)
    _, audio_kbps = initial_auto_budgets(options, media)
    commands: List[List[str]] = []
    attempts = 0

    max_attempts = 5
    if ctx is not None:
        ctx.duration = media.duration

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        cmd = build_audio_encode_command(options, audio_kbps=audio_kbps)
        commands.append(cmd)
        if ctx is not None:
            ctx.phase = f"attempt {attempt}/{max_attempts}"
            ctx.phase_start = (attempt - 1) / max_attempts
            ctx.phase_weight = 1.0 / max_attempts
            run_streaming_with_progress(cmd, ctx)
        else:
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


def run_manual(options: EncodeOptions, media: MediaInfo, ctx: Optional[ProgressContext] = None) -> ConversionResult:
    kind = output_kind(options.output_path)
    if kind == "video":
        cmd = build_video_encode_command(options, media, video_kbps=None, audio_kbps=options.audio_bitrate_kbps)
    else:
        cmd = build_audio_encode_command(options, audio_kbps=options.audio_bitrate_kbps)
    if ctx is not None:
        ctx.duration = media.duration
        ctx.phase = "encoding"
        ctx.phase_start = 0.0
        ctx.phase_weight = 1.0
        run_streaming_with_progress(cmd, ctx)
    else:
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


def run_conversion(options: EncodeOptions, ctx: Optional[ProgressContext] = None) -> ConversionResult:
    media = probe_media(options.input_path)
    validate_options(options, media)
    if options.output_path.exists() and not options.overwrite:
        raise ConversionError(f"Output file already exists: {options.output_path}")
    kind = output_kind(options.output_path)
    if options.mode == "manual":
        return run_manual(options, media, ctx)
    if kind == "video":
        return run_auto_video(options, media, ctx)
    return run_auto_audio(options, media, ctx)


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
        preview_passlog = "/tmp/shrinky-preview"
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


@dataclass
class Section:
    key: str
    title: str
    field_keys: List[str]
    expanded: bool = True


SECTIONS: List[Section] = [
    Section("source", "Source", ["input_path", "output_path"]),
    Section("target", "Target", ["mode", "target_size_mb"]),
    Section("video", "Video", ["width", "height", "fps", "video_bitrate_kbps", "crf", "preset"]),
    Section("audio", "Audio", ["include_audio", "audio_bitrate_kbps"]),
]

FIELD_HINTS = {
    "input_path": "Path to the source media file. Enter browses; e edits as text.",
    "output_path": "Where to write the encoded result. Extension drives codec.",
    "mode": "auto_size targets a file size; manual lets you pick CRF or bitrate.",
    "target_size_mb": "Target output size in megabytes (auto_size mode only).",
    "include_audio": "Keep the audio track in a video output.",
    "audio_bitrate_kbps": "Audio bitrate in kbps. Defaults vary by codec.",
    "width": "Output width in pixels. Height auto-derives if blank.",
    "height": "Output height in pixels. Width auto-derives if blank.",
    "fps": "Output frame rate. Blank keeps source fps.",
    "video_bitrate_kbps": "Manual video bitrate in kbps. Mutually exclusive with CRF.",
    "crf": "Manual quality target (lower = better, 18-28 typical).",
    "preset": "libx264 speed/efficiency tradeoff.",
}

FIELD_HELP = {
    "input_path": [
        "Source media file.",
        "Enter: open file browser.",
        "e: type a path manually.",
    ],
    "output_path": [
        "Output file. The extension picks the codec:",
        ".mp4/.mkv/.mov → h264 video.",
        ".mp3/.m4a/.opus → lossy audio.",
        ".wav/.flac → lossless audio.",
    ],
    "mode": [
        "auto_size: hits a target MB via two-pass encoding.",
        "manual: you pick CRF or bitrate, output size varies.",
    ],
    "target_size_mb": [
        "Final file size cap in MB.",
        "Common: Discord 10, WhatsApp 16, Twitter 512.",
        "Tool retries up to 5 passes to land in range.",
    ],
    "include_audio": [
        "yes: keep audio track.",
        "no: silent output. Useful for screencaps.",
    ],
    "audio_bitrate_kbps": [
        "Higher = better quality, larger file.",
        "Speech: 64-96. Music: 128-192.",
        "Opus is ~30% more efficient than mp3/aac.",
    ],
    "width": [
        "Output width in pixels.",
        "Leave blank to keep source width.",
        "If only one of width/height is set, aspect ratio is preserved.",
    ],
    "height": [
        "Output height in pixels.",
        "Leave blank to keep source height.",
        "If only one of width/height is set, aspect ratio is preserved.",
    ],
    "fps": [
        "Output frame rate.",
        "Blank: keep source fps.",
        "24-30 is standard. Lower fps shrinks file size.",
    ],
    "video_bitrate_kbps": [
        "Manual video bitrate target.",
        "Cannot be combined with CRF.",
        "1080p30: 2000-5000. 720p30: 1200-2500.",
    ],
    "crf": [
        "Constant Rate Factor (libx264 quality).",
        "Lower = better quality + larger file.",
        "18 visually lossless, 23 default, 28 small.",
    ],
    "preset": [
        "Encode speed vs compression efficiency.",
        "medium: fast, baseline efficiency.",
        "slow: ~30% smaller at ~3x encode time.",
        "veryslow: marginal extra savings for ~2x slow time.",
    ],
    "convert_button": [
        "Run the conversion.",
        "Disabled until required fields are valid.",
        "C from anywhere also triggers it.",
    ],
}

CONVERT_BUTTON_KEY = "__convert__"

FIELD_SPECS_BY_KEY = {field.key: field for field in FORM_FIELDS}

USE_UNICODE = "UTF-8" in os.environ.get("LANG", "").upper() or "UTF-8" in os.environ.get("LC_ALL", "").upper()

if USE_UNICODE:
    BOX = {
        "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
        "h": "─", "v": "│",
        "fold_open": "▼", "fold_closed": "▶",
        "cursor": "▸",
        "ok": "✓", "bad": "✗", "empty": "·", "required": "!",
    }
else:
    BOX = {
        "tl": "+", "tr": "+", "bl": "+", "br": "+",
        "h": "-", "v": "|",
        "fold_open": "v", "fold_closed": ">",
        "cursor": ">",
        "ok": "y", "bad": "x", "empty": ".", "required": "!",
    }


def is_audio_only_output(output_path_raw: str) -> bool:
    raw = output_path_raw.strip()
    if not raw:
        return False
    return Path(raw).suffix.lower() in AUDIO_OUTPUT_EXTS


def is_lossless_output(output_path_raw: str) -> bool:
    raw = output_path_raw.strip()
    if not raw:
        return False
    return Path(raw).suffix.lower() in AUTO_SIZE_AUDIO_UNSUPPORTED_EXTS


@dataclass
class ProgressView:
    log_path: Path
    log_tail: deque
    phase: str = "starting"
    fraction: float = 0.0
    speed: str = ""
    out_time_seconds: float = 0.0
    elapsed: float = 0.0
    status: str = "running"  # running | complete | failed
    error_text: str = ""


class TuiState:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.progress: Optional[ProgressView] = None
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

        persisted = load_persisted_state()
        last_input = persisted.get("last_input_path", "") if isinstance(persisted, dict) else ""
        if last_input and Path(last_input).expanduser().exists():
            self.input_path = last_input
        else:
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
            if value.strip():
                save_persisted_state({"last_input_path": value.strip()})
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

    def is_field_hidden(self, key: str) -> bool:
        audio_only = is_audio_only_output(self.output_path)
        lossless = is_lossless_output(self.output_path)
        if key == "target_size_mb":
            return self.mode != "auto_size"
        if key in ("width", "height", "fps", "preset"):
            return audio_only
        if key == "video_bitrate_kbps":
            return audio_only or self.mode == "auto_size"
        if key == "crf":
            if audio_only or self.mode == "auto_size":
                return True
            return bool(self.video_bitrate_kbps.strip())
        if key == "include_audio":
            return audio_only
        if key == "audio_bitrate_kbps":
            if not audio_only and self.include_audio == "no":
                return True
            if audio_only and lossless:
                return True
            return False
        return False

    def visible_field_keys(self) -> List[str]:
        keys: List[str] = []
        for section in SECTIONS:
            if not section.expanded:
                continue
            for field_key in section.field_keys:
                if not self.is_field_hidden(field_key):
                    keys.append(field_key)
        return keys

    def navigable_keys(self) -> List[str]:
        return self.visible_field_keys() + [CONVERT_BUTTON_KEY]

    def form_is_valid(self) -> Tuple[bool, str]:
        try:
            self.build_options()
        except Exception as exc:
            return False, str(exc)
        return True, ""

    def section_for_field(self, field_key: str) -> Optional[Section]:
        for section in SECTIONS:
            if field_key in section.field_keys:
                return section
        return None

    def validate_field(self, key: str) -> Tuple[str, str]:
        spec = FIELD_SPECS_BY_KEY[key]
        value = self.get_value(key).strip()
        if spec.kind == "cycle":
            return BOX["ok"], ""
        required_keys = {"input_path", "output_path"}
        if key == "target_size_mb" and self.mode == "auto_size":
            required_keys = required_keys | {key}
        if not value:
            if key in required_keys:
                return BOX["required"], "Required."
            return BOX["empty"], ""
        try:
            if key in ("target_size_mb", "fps"):
                parse_optional_float(value, spec.label)
            elif key in ("audio_bitrate_kbps", "width", "height", "video_bitrate_kbps", "crf"):
                parse_optional_int(value, spec.label)
            elif key in ("input_path", "output_path"):
                if key == "input_path" and not Path(value).expanduser().exists():
                    return BOX["bad"], "File not found."
        except ConversionError as exc:
            return BOX["bad"], str(exc)
        return BOX["ok"], ""

    def estimate_lines(self) -> List[str]:
        if self.mode != "auto_size":
            return ["manual mode — size depends on encode parameters"]
        try:
            options = self.build_options()
        except Exception as exc:
            return [f"unavailable: {exc}"]
        media = self.get_probe_media()
        if media is None:
            return ["unavailable: no probe"]
        try:
            video_kbps, audio_kbps = initial_auto_budgets(options, media)
        except ConversionError as exc:
            return [f"unavailable: {exc}"]
        target_mb = options.target_size_mb or 0
        lines = [f"target: {target_mb:.2f} MB"]
        if video_kbps is not None:
            lines.append(f"video budget: {video_kbps} kbps")
        if audio_kbps is not None:
            lines.append(f"audio budget: {audio_kbps} kbps")
        return lines

    def toggle_section(self, section_key: str) -> None:
        for section in SECTIONS:
            if section.key == section_key:
                section.expanded = not section.expanded
                return


COLOR_HEADER = 1
COLOR_FOCUS = 2
COLOR_OK = 3
COLOR_BAD = 4
COLOR_DIM = 5


def init_colors() -> bool:
    if not curses.has_colors():
        return False
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, bg)
    curses.init_pair(COLOR_FOCUS, curses.COLOR_YELLOW, bg)
    curses.init_pair(COLOR_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(COLOR_BAD, curses.COLOR_RED, bg)
    curses.init_pair(COLOR_DIM, curses.COLOR_WHITE, bg)
    return True


def color(pair: int) -> int:
    try:
        return curses.color_pair(pair)
    except curses.error:
        return 0


def draw_box(
    stdscr: "curses._CursesWindow",
    y: int,
    x: int,
    h: int,
    w: int,
    title: str = "",
    *,
    draw_top: bool = True,
    draw_bottom: bool = True,
) -> None:
    if h < 2 or w < 2:
        return
    height, width = stdscr.getmaxyx()
    if y + h > height or x + w > width:
        return
    try:
        if draw_top:
            top = BOX["tl"] + BOX["h"] * (w - 2) + BOX["tr"]
            stdscr.addnstr(y, x, top, w)
        for i in range(1, h - 1):
            stdscr.addnstr(y + i, x, BOX["v"], 1)
            stdscr.addnstr(y + i, x + w - 1, BOX["v"], 1)
        if draw_bottom:
            bottom = BOX["bl"] + BOX["h"] * (w - 2) + BOX["br"]
            stdscr.addnstr(y + h - 1, x, bottom, w)
        if title and draw_top:
            label = f" {title} "
            stdscr.addnstr(y, x + 2, label, max(0, w - 4), curses.A_BOLD | color(COLOR_HEADER))
    except curses.error:
        pass


def modal_input(stdscr: "curses._CursesWindow", label: str, current: str) -> Optional[str]:
    height, width = stdscr.getmaxyx()
    box_w = min(max(50, len(current) + 20), width - 4)
    box_h = 7
    y = (height - box_h) // 2
    x = (width - box_w) // 2
    win = curses.newwin(box_h, box_w, y, x)
    win.keypad(True)
    win.erase()
    draw_box(win, 0, 0, box_h, box_w, f"Edit: {label}")
    try:
        win.addnstr(2, 2, f"Current: {current or '<empty>'}", box_w - 4, color(COLOR_DIM))
        win.addnstr(box_h - 2, 2, "Enter save · Esc cancel · Ctrl-U clear", box_w - 4, curses.A_DIM)
        win.addnstr(3, 2, "> ", 2, curses.A_BOLD)
    except curses.error:
        pass
    win.refresh()

    buffer = current
    cursor = len(buffer)
    while True:
        try:
            win.move(3, 4)
            win.clrtoeol()
            win.addnstr(3, 4, buffer, box_w - 6)
            win.addnstr(3, box_w - 1, BOX["v"], 1)
            win.move(3, min(4 + cursor, box_w - 2))
        except curses.error:
            pass
        curses.curs_set(1)
        ch = win.getch()
        curses.curs_set(0)
        if ch == 27:
            return None
        if ch in (10, 13, curses.KEY_ENTER):
            return buffer.strip()
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if cursor > 0:
                buffer = buffer[: cursor - 1] + buffer[cursor:]
                cursor -= 1
            continue
        if ch == 21:  # Ctrl-U
            buffer = ""
            cursor = 0
            continue
        if ch == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
            continue
        if ch == curses.KEY_RIGHT:
            cursor = min(len(buffer), cursor + 1)
            continue
        if ch == curses.KEY_HOME:
            cursor = 0
            continue
        if ch == curses.KEY_END:
            cursor = len(buffer)
            continue
        if 32 <= ch < 127:
            buffer = buffer[:cursor] + chr(ch) + buffer[cursor:]
            cursor += 1


@dataclass
class PickerEntry:
    path: Path
    is_dir: bool
    size: Optional[int]


def list_directory(directory: Path, show_all: bool) -> List[PickerEntry]:
    entries: List[PickerEntry] = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (PermissionError, OSError):
        return entries
    for child in children:
        if child.name.startswith(".") and not show_all:
            continue
        if child.is_dir():
            entries.append(PickerEntry(child, True, None))
            continue
        if not show_all and child.suffix.lower() not in SUPPORTED_INPUT_EXTS:
            continue
        try:
            size = child.stat().st_size
        except OSError:
            size = None
        entries.append(PickerEntry(child, False, size))
    return entries


def file_picker(stdscr: "curses._CursesWindow", start: Path) -> Optional[Path]:
    height, width = stdscr.getmaxyx()
    box_w = min(max(60, width - 8), width - 2)
    box_h = min(max(14, height - 6), height - 2)
    y = (height - box_h) // 2
    x = (width - box_w) // 2
    win = curses.newwin(box_h, box_w, y, x)
    win.keypad(True)

    current_dir = start.expanduser().resolve()
    if not current_dir.exists() or not current_dir.is_dir():
        current_dir = Path.cwd()
    show_all = False
    selected = 0
    scroll = 0

    while True:
        entries = list_directory(current_dir, show_all)
        rows_visible = box_h - 5
        if selected >= len(entries):
            selected = max(0, len(entries) - 1)
        if selected < scroll:
            scroll = selected
        elif selected >= scroll + rows_visible:
            scroll = selected - rows_visible + 1

        win.erase()
        draw_box(win, 0, 0, box_h, box_w, "Pick input")
        try:
            path_text = str(current_dir)
            if len(path_text) > box_w - 4:
                path_text = "…" + path_text[-(box_w - 5):]
            win.addnstr(1, 2, path_text, box_w - 4, color(COLOR_DIM))
            footer = f"Enter open · Bksp up · . {'hide' if show_all else 'show'} hidden · Esc"
            win.addnstr(box_h - 2, 2, footer, box_w - 4, curses.A_DIM)
        except curses.error:
            pass

        parent_y = 3
        try:
            attr = curses.A_BOLD | color(COLOR_FOCUS) if selected == -1 else curses.A_NORMAL
            cursor_mark = BOX["cursor"] if selected == -1 else " "
            win.addnstr(parent_y, 2, f"{cursor_mark} ..", box_w - 4, attr)
        except curses.error:
            pass

        for offset in range(rows_visible):
            row_index = scroll + offset
            if row_index >= len(entries):
                break
            entry = entries[row_index]
            row_y = parent_y + 1 + offset
            cursor_mark = BOX["cursor"] if row_index == selected else " "
            name = entry.path.name + ("/" if entry.is_dir else "")
            size_text = "" if entry.is_dir else (human_size(entry.size) if entry.size is not None else "?")
            label = f"{cursor_mark} {name}"
            attr = curses.A_BOLD | color(COLOR_FOCUS) if row_index == selected else curses.A_NORMAL
            try:
                win.addnstr(row_y, 2, label, box_w - 4 - len(size_text) - 1, attr)
                if size_text:
                    win.addnstr(row_y, box_w - 2 - len(size_text), size_text, len(size_text), attr | curses.A_DIM)
            except curses.error:
                pass

        win.refresh()
        ch = win.getch()
        if ch == 27:
            return None
        if ch in (curses.KEY_UP, ord("k")):
            selected = max(-1, selected - 1)
            continue
        if ch in (curses.KEY_DOWN, ord("j")):
            selected = min(len(entries) - 1, selected + 1)
            continue
        if ch == curses.KEY_PPAGE:
            selected = max(-1, selected - rows_visible)
            continue
        if ch == curses.KEY_NPAGE:
            selected = min(len(entries) - 1, selected + rows_visible)
            continue
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if current_dir.parent != current_dir:
                current_dir = current_dir.parent
                selected = 0
                scroll = 0
            continue
        if ch == ord("."):
            show_all = not show_all
            selected = 0
            scroll = 0
            continue
        if ch == ord("~"):
            current_dir = Path.home()
            selected = 0
            scroll = 0
            continue
        if ch == ord("/"):
            current_dir = Path("/")
            selected = 0
            scroll = 0
            continue
        if ch in (10, 13, curses.KEY_ENTER):
            if selected == -1:
                if current_dir.parent != current_dir:
                    current_dir = current_dir.parent
                    selected = 0
                    scroll = 0
                continue
            if not entries:
                continue
            entry = entries[selected]
            if entry.is_dir:
                current_dir = entry.path
                selected = 0
                scroll = 0
                continue
            return entry.path


PROGRESS_PANE_MIN_H = 8
PROGRESS_LOG_LINES = 4


def render_progress_pane(
    stdscr: "curses._CursesWindow",
    progress: ProgressView,
    y: int,
    x: int,
    h: int,
    w: int,
) -> None:
    if h < 4 or w < 10:
        return
    title = {"running": "Converting", "complete": "Done", "failed": "Failed"}.get(progress.status, "Progress")
    draw_box(stdscr, y, x, h, w, title)
    inner_x = x + 2
    inner_w = w - 4

    fraction = max(0.0, min(1.0, progress.fraction))
    bar_w = max(8, inner_w - 8)
    filled = int(round(bar_w * fraction))
    bar = "=" * filled + "-" * (bar_w - filled)
    pct = f"{int(fraction * 100):3d}%"

    phase_line = progress.phase or progress.status
    speed_bits: List[str] = []
    if progress.speed:
        speed_bits.append(f"speed {progress.speed}")
    if progress.out_time_seconds:
        speed_bits.append(f"t {progress.out_time_seconds:5.1f}s")
    speed_bits.append(f"elapsed {progress.elapsed:5.1f}s")
    speed_line = " · ".join(speed_bits)

    log_label = f"log: {_truncate_middle(str(progress.log_path), max(0, inner_w - 5))}"

    try:
        stdscr.addnstr(y + 1, inner_x, phase_line, inner_w, color(COLOR_HEADER) | curses.A_BOLD)
        stdscr.addnstr(y + 2, inner_x, f"[{bar}] {pct}", inner_w)
        stdscr.addnstr(y + 3, inner_x, speed_line, inner_w, curses.A_DIM)
        stdscr.addnstr(y + 4, inner_x, log_label, inner_w, color(COLOR_DIM))
    except curses.error:
        pass

    log_top = y + 5
    log_h = max(0, h - 6)
    if log_h > 0:
        try:
            stdscr.addnstr(log_top, inner_x, "-" * inner_w, inner_w, curses.A_DIM)
        except curses.error:
            pass
        tail = list(progress.log_tail)[-log_h:]
        for offset in range(log_h):
            line_y = log_top + 1 + offset
            if line_y >= y + h - 1:
                break
            idx = offset - (log_h - len(tail))
            text = tail[idx] if 0 <= idx < len(tail) else ""
            text = text.replace("\t", " ")
            if len(text) > inner_w:
                text = "…" + text[-(inner_w - 1):]
            try:
                stdscr.addnstr(line_y, inner_x, text.ljust(inner_w), inner_w, curses.A_DIM)
            except curses.error:
                pass


def render_form_pane(
    stdscr: "curses._CursesWindow",
    state: TuiState,
    selected_key: Optional[str],
    y: int,
    x: int,
    h: int,
    w: int,
) -> None:
    progress_h = 0
    if state.progress is not None:
        log_lines = PROGRESS_LOG_LINES if state.progress.status == "running" else PROGRESS_LOG_LINES + 2
        progress_h = min(h - 6, 6 + log_lines)
        progress_h = max(PROGRESS_PANE_MIN_H, progress_h)
    form_h = h - progress_h

    draw_box(stdscr, y, x, form_h, w, "Form")
    inner_x = x + 2
    inner_w = w - 4
    cursor = y + 1
    bottom = y + form_h - 1
    for section in SECTIONS:
        if cursor >= bottom:
            break
        marker = BOX["fold_open"] if section.expanded else BOX["fold_closed"]
        title = f"{marker} {section.title}"
        try:
            stdscr.addnstr(cursor, inner_x, title, inner_w, curses.A_BOLD | color(COLOR_HEADER))
        except curses.error:
            pass
        cursor += 1
        if not section.expanded:
            continue
        for field_key in section.field_keys:
            if cursor >= bottom:
                break
            if state.is_field_hidden(field_key):
                continue
            spec = FIELD_SPECS_BY_KEY[field_key]
            value = state.get_value(field_key) or "<empty>"
            glyph, _ = state.validate_field(field_key)
            focused = field_key == selected_key
            cursor_mark = BOX["cursor"] if focused else " "
            label = f"{cursor_mark} {spec.label:>12}: "
            row_attr = curses.A_BOLD | color(COLOR_FOCUS) if focused else curses.A_NORMAL
            glyph_attr = color(COLOR_OK) if glyph == BOX["ok"] else color(COLOR_BAD) if glyph in (BOX["bad"], BOX["required"]) else curses.A_DIM
            try:
                stdscr.addnstr(cursor, inner_x, label, inner_w, row_attr)
                value_x = inner_x + len(label)
                value_w = max(0, inner_w - len(label) - 2)
                stdscr.addnstr(cursor, value_x, value, value_w, row_attr)
                stdscr.addnstr(cursor, inner_x + inner_w - 1, glyph, 1, glyph_attr)
            except curses.error:
                pass
            cursor += 1

    if cursor < bottom:
        cursor += 1  # blank spacer
        valid, _ = state.form_is_valid()
        focused = selected_key == CONVERT_BUTTON_KEY
        label = "[ Convert ]"
        if focused and valid:
            attr = curses.A_BOLD | curses.A_REVERSE | color(COLOR_OK)
        elif valid:
            attr = curses.A_BOLD | color(COLOR_OK)
        else:
            attr = curses.A_DIM
        button_x = inner_x + max(0, (inner_w - len(label)) // 2)
        try:
            stdscr.addnstr(cursor, button_x, label, inner_w, attr)
        except curses.error:
            pass

    if state.progress is not None and progress_h > 0:
        render_progress_pane(stdscr, state.progress, y + form_h, x, progress_h, w)


def render_info_pane(
    stdscr: "curses._CursesWindow",
    title: str,
    lines: Sequence[str],
    y: int,
    x: int,
    h: int,
    w: int,
    *,
    draw_top: bool = True,
    draw_bottom: bool = True,
) -> None:
    draw_box(stdscr, y, x, h, w, title, draw_top=draw_top, draw_bottom=draw_bottom)
    inner_x = x + 2
    inner_w = w - 4
    wrapped = wrap_lines(list(lines), inner_w)
    for offset, line in enumerate(wrapped[: h - 2]):
        try:
            stdscr.addnstr(y + 1 + offset, inner_x, line, inner_w)
        except curses.error:
            pass


def draw_tui(stdscr: "curses._CursesWindow", state: TuiState, selected_key: Optional[str]) -> None:
    stdscr.clear()
    height, width = stdscr.getmaxyx()

    title = "Shrinky"
    try:
        stdscr.addnstr(0, 1, title, width - 2, curses.A_BOLD | color(COLOR_HEADER))
        help_line = "↑↓ move · Enter edit/browse · ←→ cycle · Space fold · C convert · Q quit"
        stdscr.addnstr(1, 1, help_line, width - 2, curses.A_DIM)
    except curses.error:
        pass

    body_top = 2
    body_bottom = height - 3
    body_h = max(5, body_bottom - body_top)
    left_w = max(38, width // 2)
    right_x = left_w
    right_w = max(20, width - right_x - 1)

    render_form_pane(stdscr, state, selected_key, body_top, 0, body_h, left_w)

    inner_w = max(10, right_w - 4)
    summary_lines = state.media_summary()
    help_lines = help_lines_for(state, selected_key)
    estimate_lines = state.estimate_lines()
    preview_lines = state.preview_lines(inner_w)

    # Each pane carries its own top + bottom border. Adjacent panes
    # are separated by one blank row to avoid border collisions.
    gap = 1
    pane_count = 4
    summary_h = min(max(4, len(wrap_lines(summary_lines, inner_w)) + 2), max(4, body_h // 3))
    help_h = min(max(4, len(wrap_lines(help_lines, inner_w)) + 2), 12)
    estimate_h = max(4, len(wrap_lines(estimate_lines, inner_w)) + 2)
    min_preview_h = 4

    def total_rows() -> int:
        return summary_h + help_h + estimate_h + min_preview_h + gap * (pane_count - 1)

    while total_rows() > body_h:
        if help_h > 4:
            help_h -= 1
        elif summary_h > 4:
            summary_h -= 1
        elif estimate_h > 4:
            estimate_h -= 1
        else:
            break

    preview_h = max(min_preview_h, body_h - (summary_h + help_h + estimate_h + gap * (pane_count - 1)))

    y0 = body_top
    y1 = y0 + summary_h + gap
    y2 = y1 + help_h + gap
    y3 = y2 + estimate_h + gap

    render_info_pane(stdscr, "Input", summary_lines, y0, right_x, summary_h, right_w)
    render_info_pane(stdscr, "Help", help_lines, y1, right_x, help_h, right_w)
    render_info_pane(stdscr, "Estimate", estimate_lines, y2, right_x, estimate_h, right_w)
    render_info_pane(stdscr, "Command preview", preview_lines, y3, right_x, preview_h, right_w)

    hint = ""
    if selected_key == CONVERT_BUTTON_KEY:
        valid, reason = state.form_is_valid()
        hint = "Press Enter or C to run." if valid else f"Cannot convert: {reason}"
    elif selected_key:
        glyph, reason = state.validate_field(selected_key)
        base_hint = FIELD_HINTS.get(selected_key, "")
        if reason:
            hint = f"{base_hint}  ({reason})" if base_hint else reason
        else:
            hint = base_hint

    try:
        stdscr.move(height - 2, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(height - 2, 1, hint, width - 2, curses.A_DIM)
        stdscr.move(height - 1, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(height - 1, 1, state.message, width - 2, color(COLOR_HEADER))
    except curses.error:
        pass
    stdscr.refresh()


def move_selection(state: TuiState, current: Optional[str], delta: int) -> Optional[str]:
    keys = state.navigable_keys()
    if not keys:
        return None
    if current not in keys:
        return keys[0]
    index = keys.index(current)
    return keys[(index + delta) % len(keys)]


def help_lines_for(state: TuiState, selected_key: Optional[str]) -> List[str]:
    if selected_key is None:
        return ["Move with ↑↓ or Tab.", "Enter edits, ←→ cycles options."]
    if selected_key == CONVERT_BUTTON_KEY:
        lines = list(FIELD_HELP.get("convert_button", []))
        valid, reason = state.form_is_valid()
        if not valid:
            lines.append("")
            lines.append(f"Blocked: {reason}")
        return lines
    return list(FIELD_HELP.get(selected_key, []))


def _truncate_middle(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    keep = width - 1
    head = keep // 2
    tail = keep - head
    return text[:head] + "…" + text[-tail:]


def trigger_convert(stdscr: "curses._CursesWindow", state: TuiState) -> None:
    valid, reason = state.form_is_valid()
    if not valid:
        state.message = reason
        return
    options = state.build_options()

    log_path = create_log_file()
    log_tail: deque = deque(maxlen=200)
    cancel_event = threading.Event()
    update_lock = threading.Lock()

    progress = ProgressView(log_path=log_path, log_tail=log_tail, phase="starting")
    state.progress = progress

    def on_progress(update: ProgressUpdate) -> None:
        with update_lock:
            progress.phase = update.phase
            progress.fraction = update.fraction
            progress.speed = update.speed
            progress.out_time_seconds = update.out_time_seconds

    ctx = ProgressContext(
        log_path=log_path,
        callback=on_progress,
        cancel_event=cancel_event,
    )
    original_append = ctx.append_log

    def append_with_tail(text: str) -> None:
        original_append(text)
        for piece in text.splitlines():
            if piece.strip():
                log_tail.append(piece)

    ctx.append_log = append_with_tail  # type: ignore[assignment]

    result_box: dict = {}

    def worker() -> None:
        try:
            result_box["result"] = run_conversion(options, ctx)
        except Exception as exc:  # noqa: BLE001
            result_box["error"] = exc

    log_path.write_text(f"shrinky log {datetime.now().isoformat()}\n", encoding="utf-8")
    log_tail.append(f"started at {datetime.now().strftime('%H:%M:%S')}")

    state.message = f"Converting… (Q to cancel)  log: {log_path}"
    thread = threading.Thread(target=worker, daemon=True)
    started = time.monotonic()
    thread.start()

    stdscr.nodelay(True)
    try:
        while thread.is_alive():
            with update_lock:
                progress.elapsed = time.monotonic() - started
            draw_tui(stdscr, state, CONVERT_BUTTON_KEY)
            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                ch = 27
            if ch in (ord("q"), ord("Q"), 27):
                if not cancel_event.is_set():
                    cancel_event.set()
                    log_tail.append("cancel requested")
                    state.message = f"Cancel requested…  log: {log_path}"
            time.sleep(0.1)
        thread.join(timeout=10.0)
        if thread.is_alive():
            cancel_event.set()
            thread.join(timeout=5.0)
    finally:
        stdscr.nodelay(False)

    progress.elapsed = time.monotonic() - started
    if "error" in result_box:
        err = result_box["error"]
        progress.status = "failed"
        progress.phase = "failed"
        progress.error_text = str(err)
        log_tail.append(f"failed: {err}")
        state.message = f"Failed: {err}  log: {log_path}  (Enter to dismiss)"
    else:
        result: ConversionResult = result_box["result"]
        progress.status = "complete"
        progress.phase = "complete"
        progress.fraction = 1.0
        log_tail.append(f"done — {result.output_path.name} {human_size(result.size_bytes)}")
        state.message = f"Finished: {result.output_path.name} ({human_size(result.size_bytes)})  log: {log_path}  (Enter to dismiss)"

    draw_tui(stdscr, state, CONVERT_BUTTON_KEY)


def run_tui(stdscr: "curses._CursesWindow") -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    init_colors()
    state = TuiState(Path.cwd())
    keys = state.navigable_keys()
    selected_key: Optional[str] = keys[0] if keys else None

    while True:
        if selected_key is not None and selected_key != CONVERT_BUTTON_KEY and state.is_field_hidden(selected_key):
            selected_key = move_selection(state, selected_key, 1)

        draw_tui(stdscr, state, selected_key)
        key = stdscr.getch()

        if state.progress is not None and state.progress.status != "running":
            if key in (10, 13, curses.KEY_ENTER, 27, ord(" "), ord("q"), ord("Q")):
                state.progress = None
                state.message = "Press Convert to start another, or Q to quit."
                continue

        if key in (ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            selected_key = move_selection(state, selected_key, -1)
            continue
        if key in (curses.KEY_DOWN, ord("j"), 9):
            selected_key = move_selection(state, selected_key, 1)
            continue
        if key in (ord("c"), ord("C")):
            trigger_convert(stdscr, state)
            continue
        if selected_key == CONVERT_BUTTON_KEY:
            if key in (10, 13, curses.KEY_ENTER, ord(" ")):
                trigger_convert(stdscr, state)
            continue
        if key == ord(" "):
            if selected_key:
                spec = FIELD_SPECS_BY_KEY[selected_key]
                if spec.kind == "cycle":
                    state.cycle(selected_key, 1)
                    continue
                section = state.section_for_field(selected_key)
                if section:
                    state.toggle_section(section.key)
            continue
        if selected_key is None:
            continue
        spec = FIELD_SPECS_BY_KEY[selected_key]

        if key == curses.KEY_LEFT:
            if spec.kind == "cycle":
                state.cycle(selected_key, -1)
            continue
        if key == curses.KEY_RIGHT:
            if spec.kind == "cycle":
                state.cycle(selected_key, 1)
            continue
        if key in (10, 13, curses.KEY_ENTER):
            if spec.kind == "cycle":
                state.cycle(selected_key, 1)
                continue
            if selected_key == "input_path":
                current_value = state.get_value("input_path").strip()
                start_dir = Path(current_value).expanduser().parent if current_value else Path.cwd()
                picked = file_picker(stdscr, start_dir)
                if picked is not None:
                    state.set_value("input_path", str(picked))
                    state.message = f"Selected {picked.name}"
                continue
            new_value = modal_input(stdscr, spec.label, state.get_value(selected_key))
            if new_value is not None:
                state.set_value(selected_key, new_value)
                state.message = f"Updated {spec.label}."
            continue
        if key in (ord("e"), ord("E")) and selected_key == "input_path":
            new_value = modal_input(stdscr, spec.label, state.get_value(selected_key))
            if new_value is not None:
                state.set_value(selected_key, new_value)
                state.message = f"Updated {spec.label}."
            continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shrinky — terminal media shrinker."
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
    parser.add_argument("--crf", type=int, default=None)
    parser.add_argument("--preset", choices=PRESET_OPTIONS, default="slow")
    parser.add_argument("--no-audio", action="store_true", help="Strip audio from the output.")
    parser.add_argument("--no-overwrite", action="store_true", help="Fail instead of overwriting an existing output file.")
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
        crf=args.crf,
        preset=args.preset,
        overwrite=not args.no_overwrite,
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
        ensure_dependencies()
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

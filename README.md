# Shrinky

A terminal media shrinker. Give it an audio or video file and a target size, and it encodes an output that lands inside that budget. A single Python file with a curses TUI and a scriptable CLI; the only dependency is `ffmpeg`. Audio and video only — no images, PDFs, or archives.

> Read the story behind it in [ARTICLE.md](ARTICLE.md).

## Requirements

- Python 3.8+
- `ffmpeg` and `ffprobe` on your `PATH`

## Install

```bash
git clone https://github.com/rbmrs/shrinky.git
cd shrinky
ln -s "$PWD/app.py" ~/.local/bin/shrinky
```

## Use

```bash
shrinky              # launch the TUI (bare invocation)
shrinky --help       # CLI flags for scripts and pipelines
```

## CLI flags

| Flag | Default | What it does |
| --- | --- | --- |
| `--input` / `--output` | — | Input and output media files |
| `--mode {auto_size,manual}` | `auto_size` | Hit a size target, or drive the encode by hand |
| `--target-size-mb` | `10` | Auto-size target, in MB |
| `--width` / `--height` / `--fps` | source | Resize / reframe. Set width alone to preserve aspect ratio |
| `--preset {medium,slow,veryslow}` | `slow` | Encode effort: slower trades CPU for quality at the same size |
| `--crf` | `23` | Manual mode: quality-targeted encode (mutually exclusive with bitrate) |
| `--video-bitrate-kbps` | — | Manual mode: explicit video bitrate |
| `--audio-bitrate-kbps` | `128` (`96` for opus) | Override audio bitrate |
| `--no-audio` | off | Drop the audio track |
| `--no-overwrite` | off | Fail instead of replacing an existing output |
| `--dry-run` | off | Print the `ffmpeg` command and exit without encoding |
| `--probe-json` / `--preview-json` / `--progress-json` | — | Structured output for callers driving Shrinky from another program |

**auto-size** iterates the bitrate until the output lands inside the target (h264, two-pass). **manual** mode hands you the encoder knobs directly. Lossless codecs (`.wav`, `.flac`) refuse auto-size — there's no bitrate to scale.

## macOS app (beta)

A native macOS shell lives in `macos/`. Prebuilt beta builds are on the [Releases page](https://github.com/rbmrs/shrinky/releases): each push to `main` ships a new `Shrinky-<version>-macos.zip`. The builds are unsigned — on first launch, right-click `Shrinky.app` and choose **Open** to get past Gatekeeper.

Build it yourself:

```bash
scripts/build_macos_app.sh
open .build/debug/Shrinky.app
```

Set `SHRINKY_BACKEND=/path/to/app.py` if launching outside the repo.

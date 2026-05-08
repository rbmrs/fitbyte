# Shrinky

Terminal media shrinker.

Backed by `ffmpeg` and `ffprobe`. Hits a target size or runs a manual encode, all from a TUI.

## Screenshot

![Shrinky TUI screenshot](docs/shrinky-tui.png)

## Run

```bash
shrinky
```

## macOS prototype

```bash
scripts/build_macos_app.sh
open .build/debug/Shrinky.app
```

Set `SHRINKY_BACKEND=/path/to/app.py` if you launch the app outside the repo.

## Help

```bash
shrinky --help
```

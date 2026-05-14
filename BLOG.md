# Shrinky — Blog Sketch

Working notes for a blog post about `shrinky`, a small terminal media
converter built on top of `ffmpeg` and `ffprobe`. Use this as a skeleton —
fill in voice, screenshots, anecdotes, and benchmarks where marked `[TODO]`.

---

## Suggested titles

- *"Shrinky: a 1,000-line terminal app that hits a target file size on the first try"*
- *"I built a curses TUI for ffmpeg so I'd stop googling the same flags"*
- *"Auto-sizing video with two-pass ffmpeg, in under 1,000 lines of Python"*

Pick one once the angle of the post is decided.

---

## Opening hook (200–300 words)

The problem in plain language: every time you need to share a video on a
platform with a hard size cap (Discord 10 MB, WhatsApp 16 MB, Twitter
512 MB), you end up reaching for ffmpeg, googling the right flags, picking
a bitrate that *looks* right, encoding for two minutes, finding out the
output is 11.4 MB, tweaking, re-encoding, repeat.

Most GUI converters either hide ffmpeg entirely or expose every knob with
no opinion. `shrinky` sits in between: a small terminal app that
either does the size math for you, or gets out of the way when you want
manual control.

`[TODO]` Personal anecdote — the specific time you got frustrated enough
to build this. Concrete numbers (file size, time wasted) land harder than
generic complaints.

---

## What it is

- One Python file (`app.py`, ~1,000 lines), no dependencies beyond the
  standard library.
- Wraps `ffmpeg` and `ffprobe`. Doesn't reimplement encoding — just makes
  the right command line easy to assemble.
- Two interfaces: a curses TUI (default when launched with no args), and
  a flag-driven CLI for scripts and pipelines.
- Two modes:
  - **auto-size** — you pick a target file size in MB, the tool runs a
    two-pass encode and iterates the bitrate until the output lands in
    range.
  - **manual** — you pick CRF or video bitrate, audio bitrate,
    resolution, fps, preset; the tool just builds the command.

---

## The layout (Path B)

`[TODO]` Drop a screenshot of the new TUI here.

The screen is split into a left form pane and a right info pane.

### Left pane — form, grouped into four collapsible sections

```
▼ Source
    Input path
    Output path
▼ Target
    Mode                (auto_size | manual)
    Target size MB      [auto_size only]
▼ Video                 [hidden when output is audio-only]
    Width
    Height
    FPS
    Video kbps          [manual only]
    CRF                 [manual only, hidden when Video kbps is set]
    Preset
▼ Audio
    Keep audio          [video output only]
    Audio kbps          [hidden when audio is off or codec is lossless]
```

Why grouped + collapsible: the original layout showed all twelve fields
flat. Half of them were irrelevant depending on the mode and output
format. Grouping makes the schema legible; hiding the irrelevant ones
removes decision fatigue.

Each value carries an inline status glyph:
- `✓` parsed and within range
- `✗` invalid (with a one-line reason in the footer hint)
- `·` empty optional field
- `!` empty required field

### Right pane — three stacked info boxes

1. **Input** — what `ffprobe` saw: size, duration, codecs, dimensions,
   fps. Refreshes when the input path changes.
2. **Estimate** — in auto-size mode, the computed video and audio
   bitrate budget for the chosen target. In manual mode, a note that
   size depends on encode parameters.
3. **Command preview** — the exact `ffmpeg` command(s) that will run,
   verbatim. Two lines for two-pass encodes.

### Footer

Two rows: the key legend (`↑↓ move · Enter edit · ←→ cycle · Space
fold · C convert · Q quit`) and a contextual hint for the focused field.

### Modal input

`Enter` on a text field opens a centered modal — borders, current value,
a single input line, key hints. Replaces the cramped bottom-line prompt
the previous version used.

`[TODO]` Screenshot of the modal.

### Why this is better than the flat form

- Twelve fields → four named groups you can fold.
- Mode-irrelevant fields disappear instead of taking up vertical space
  with confusing values.
- Validation is visible at a glance — no more hitting `C` to find out
  CRF was a typo.
- Live estimate makes the auto-size loop feel less like a black box.

---

## How it works

The internal pipeline is short:

1. **Probe.** `ffprobe -print_format json -show_entries
   format=...:stream=...` parses to a `MediaInfo` dataclass: duration,
   size, codecs, dimensions, fps.
2. **Validate.** `validate_options` cross-checks the form: input exists,
   output extension is supported, audio-only output requires audio,
   auto-size needs a non-zero duration and a target, lossless audio
   codecs reject auto-size, manual mode rejects bitrate+CRF together.
3. **Build.** `build_video_encode_command` /
   `build_audio_encode_command` assemble the argv list. Filters
   (`scale=`, `fps=`) are joined into a `-vf` chain. Container quirks
   (`+faststart` on `.mp4`/`.mov`/`.m4a`) are applied automatically.
4. **Run.** `run_streaming` shells out to `ffmpeg` and inherits stdio so
   you see the progress bar.

### Auto-size: how the size math actually works

`[TODO]` Probably the most interesting section for a technical reader —
walk through with real numbers.

The naïve approach is `bitrate = target_size / duration`. That misses
twice: container overhead, and the fact that a constant-bitrate target
with a single pass produces wide variance.

What `shrinky` does:

```
target_bytes = target_mb * 1_000_000
usable_bytes = target_bytes * 0.985            # leave 1.5% for container
total_kbps   = usable_bytes * 8 / duration / 1000
video_kbps   = total_kbps - audio_kbps
```

Then a two-pass encode (`-pass 1` to `/dev/null`, then `-pass 2`). Two
passes lets `libx264` allocate bits intelligently across the timeline so
the constant *average* bitrate produces a near-constant *file size*.

After pass 2, the actual size is measured. If it's within 96.5%–100% of
target, done. Otherwise the bitrate is scaled by `target / actual` (with
a 0.985 safety margin) and the whole two-pass is rerun. Up to five
attempts.

Why 96.5% and not 100%? `libx264`'s VBV behaviour means asking for
exactly the target tends to over-shoot by 1–3%. A 1.5% headroom on the
ask consistently lands inside the budget on the first try for most
inputs.

`[TODO]` Insert a small benchmark table — a few real videos at different
durations and resolutions, showing target vs first-pass actual.

### Audio is similar but simpler

For audio-only outputs, no two-pass is needed. The same iterative
loop runs single-pass `libmp3lame` / `aac` / `libopus` and rescales the
bitrate if the file misses the target.

Lossless codecs (`pcm_s16le` for `.wav`, `flac` for `.flac`) refuse
auto-size at validation time — there's no bitrate knob to turn.

### Manual mode

`-crf 23 -preset slow` is the default for video. CRF and explicit
bitrate are mutually exclusive (the tool now errors if both are set).
Audio bitrate defaults to a sensible value per codec (96 for `.opus`,
128 elsewhere) when omitted.

---

## How to use it

### Install

```bash
git clone <repo>
cd shrinky
ln -s "$PWD/app.py" ~/.local/bin/shrinky
chmod +x app.py
```

Requires Python 3.8+ and `ffmpeg` on `PATH`. The tool checks for both at
startup and exits with a clear error if either is missing.

### TUI: shrink a video to 10 MB

```bash
shrinky
```

Drop into the curses interface. If the current directory has exactly one
media file, the input field is pre-filled. Output path is suggested as
`<name>_converted.mp4`. Tab through the form, set `Target size MB` to
`10`, press `C`. The tool runs two passes, iterates if needed, prints
the result.

### CLI: same thing, scriptable

```bash
shrinky \
  --input lecture.mov \
  --output lecture_10mb.mp4 \
  --mode auto_size \
  --target-size-mb 10
```

### Manual encode with explicit knobs

```bash
shrinky \
  --input source.mov \
  --output out.mp4 \
  --mode manual \
  --crf 20 \
  --preset slow \
  --width 1280
```

Height is derived to keep aspect ratio (`scale=1280:-2`).

### Audio extraction

```bash
shrinky \
  --input lecture.mp4 \
  --output lecture.mp3 \
  --mode auto_size \
  --target-size-mb 5
```

Output extension drives codec and container.

### Dry run — see the command without encoding

```bash
shrinky --input in.mov --output out.mp4 --dry-run
```

Useful for double-checking the assembled `ffmpeg` invocation, or for
copy-pasting it into a script.

### Don't overwrite

```bash
shrinky ... --no-overwrite
```

Errors instead of clobbering an existing output.

---

## Things I'd do differently next time

`[TODO]` Honest section — readers like postmortems.

- Would probably reach for **Textual** (modern Python TUI framework) over
  `curses` if I were starting over. Box-drawing, colors, mouse, focus
  management all become free. The cost is one dependency.
- The bitrate-scaling loop converges in 1–2 iterations for almost
  every input. The 5-attempt cap was paranoid — 3 would do.
- Auto-size for `.webm` / VP9 would be a nice add. Currently h264 only
  for video.
- Tests landed late. `tests/test_app.py` covers validation and the
  no-overwrite / auto-retry paths, but the pure functions that would be
  easiest to pin down — `initial_auto_budgets`, `scale_bitrate`, the
  command builders — are still uncovered. Should have started there.

---

## Closing

`[TODO]` Tie back to the opening hook — what changed in your day-to-day
workflow because this tool exists. Numbers if you have them ("I used to
spend ~10 min per share-a-video task; now it's 30 seconds").

Source: <https://github.com/rbmrs/shrinky>. ~1,000 lines of Python, MIT,
no dependencies beyond `ffmpeg` itself.

---

## Outline checklist for the actual post

- [ ] Opening hook with concrete frustration
- [ ] Screenshot of the TUI (post-Path B)
- [ ] Section: what it is (5 bullet points max)
- [ ] Section: layout walk-through with annotated screenshot
- [ ] Section: how auto-size works (the math + two-pass)
- [ ] Mini-benchmark table
- [ ] Section: usage examples (TUI + 3–4 CLI snippets)
- [ ] Section: tradeoffs / what I'd change
- [ ] Closing with repo link

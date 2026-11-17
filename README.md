# Codex Russian Subtitles for VLC

Translate embedded or external subtitles into Russian with your Codex CLI account.
Choose a source in any language and optionally add synchronized translations as
references. Text formatting and timing are protected; unclear captions are kept
in their original language or as original subtitle images and listed in a report.

[Инструкция на русском](README.ru.md) · [Download](https://github.com/mavaurel/vlc-codex-subs/releases/latest) · [Changelog](CHANGELOG.md)

## What it supports

| Source | Platform | Output |
| --- | --- | --- |
| SRT, ASS/SSA, WebVTT; external or embedded text tracks | macOS, Linux | Same text format; some container tracks extract to SRT |
| Embedded PGS or external `.sup` | macOS 13+ | ASS with OCR, fitted Russian text and original-image fallback |
| Local unencrypted DVD folders with `VIDEO_TS` | macOS; optional reader build | ASS, with DVD title and chapter timing |

Windows, standalone VobSub, DVD subtitle tracks inside ordinary video containers,
physical/encrypted discs and burned-in captions are not supported by the extension.
Original bitmap fonts, colors and italics cannot be reconstructed exactly.
Translation and OCR can still contain mistakes; the automatic review is not a
substitute for human verification.

## Install

Requires **VLC 3**, **Python 3.10+**, **Codex CLI** with a working login, and
**FFmpeg/FFprobe** for embedded tracks. No Python packages are required at runtime.

1. Install [VLC](https://www.videolan.org/vlc/), Python and FFmpeg. On a Mac with
   [Homebrew](https://brew.sh/):

   ```sh
   brew install python ffmpeg
   brew install --cask vlc
   ```

2. Install [Codex CLI using its official instructions](https://github.com/openai/codex#installing-and-running-codex-cli).
   For an existing Node.js/npm installation:

   ```sh
   npm install -g @openai/codex
   codex login
   ```

3. Download and extract `vlc-codex-subs-1.6.1.zip` from the
   [latest release](https://github.com/mavaurel/vlc-codex-subs/releases/latest).
   In a terminal, open the extracted directory and install:

   ```sh
   cd /path/to/vlc-codex-subs-1.6.1
   python3 install.py
   ```

   On macOS, `Install.command` in that folder runs the same installer. If Finder
   blocks opening a downloaded script, use the terminal command above.
   A nonstandard Codex location can be passed with `--codex /absolute/path/to/codex`.

4. Restart VLC. On macOS open **VLC media player → Extensions → Codex Russian
   Subtitles**; on Linux use the **View** menu.

The installer writes only to your user account and stores absolute executable
paths so VLC launched from the Dock can find them. Keep the selected Python and
Codex installations available. Re-run the installer if you move them.

For **PGS OCR**, macOS 13+ and Apple Command Line Tools are also required:

```sh
xcode-select --install
```

The first OCR run compiles the local Swift helper. OCR and font measurement run
locally. The installation ZIP contains source, not precompiled native helpers.

## Use

1. Open the video and the extension. Sources are detected automatically.
2. Choose the main **Subtitles to translate** source.
3. Optionally **Add reference** sources from the same film/episode, cut and timeline.
   References start at **None**. Overlap is checked; sparse or unsynchronized sources
   are rejected. Timestamp overlap cannot prove that a file is the correct film.
4. Click **Create Russian subtitles**. Playback can continue.
5. Click **Refresh progress** to see the latest stage or load the result.

Completed subtitles are saved beside the video when writable, plus a permanent
job copy and `review.json`. Existing files and source subtitles are preserved.
The helper loads a result only when the matching video is open. Switching videos
or closing VLC does not cancel the worker. Use **Cancel translation** to stop it;
OCR evidence, drafts and reviewed checkpoints remain available for matching retries.
Reopening the extension recovers the current video's own job.

### Speed and fidelity

**Light** defaults to `gpt-6.1-sol`, low reasoning, 64-caption batches and four
parallel workers. **Careful** uses medium reasoning and one worker with 32-caption
batches. Both translate and then review every batch. Select a model available to
your account in **Model settings** if the default is unavailable.

Flagged captions get a focused retry with neighboring source dialogue and a
separate confirmation. Unresolved text stays untranslated. Unresolved bitmap
captions retain their original glyph shapes as ASS drawings. Cue IDs and reasons
are recorded in `originals_kept` in the review report. The code does not clear
model uncertainty flags just to finish. These checks reduce errors without
claiming perfect fidelity.

For higher concurrency or Fast service, edit the installed `config.json` before
starting a new job. Upgrades preserve your settings:

```json
{
  "model": "gpt-6.1-sol",
  "profile": "light",
  "workers": 15,
  "chunk_size": 64,
  "service_tier": "fast",
  "keep_original": true,
  "save_beside_video": true
}
```

Allowed workers: 1–15. Defaults are four workers and standard service. More workers
can trigger rate limits; shared backoff automatically reduces concurrency. Fast
availability and account usage depend on Codex and your plan; see the
[official speed guide](https://learn.chatgpt.com/docs/agent-configuration/speed).
No fixed completion time is promised. `chunk_size: 0` opts into automatic larger
requests; incomplete or timed-out requests split into smaller batches.

### Privacy and saved files

Selected subtitle text and flagged subtitle image crops are sent through your
Codex account for translation and review. Ordinary unflagged bitmap images stay
local. Your account's usage limits apply. The helper invokes `codex exec` with
structured output and disabled shell, browser and app tools; it installs no server
or listening port. It uses your existing Codex login.

On macOS:

```text
~/Library/Application Support/VLC Codex Subtitles/
  config.json
  app/codex_subs/
  cache/
  jobs/<job-id>/Russian.srt  (or .ass/.ssa/.vtt)
  jobs/<job-id>/review.json
```

The extension is installed at
`~/Library/Application Support/org.videolan.vlc/lua/extensions/codex_russian_subtitles.lua`.
Linux uses `$XDG_DATA_HOME/vlc/lua/extensions` and
`$XDG_DATA_HOME/vlc-codex-subtitles`, defaulting to `~/.local/share`.

## Optional DVD folders

On macOS, build the DVD reader **before** running the installer. This optional
build requires Python **3.12+**, Apple Command Line Tools and Homebrew dependencies:

```sh
brew install libdvdnav libdvdread pkg-config
python3 setup_dvd.py
python3 install.py
```

The script downloads checksum-pinned FFmpeg source, applies the recorded DVD
chapter timestamp correction, and builds a dedicated reader. Your installed
FFmpeg is unchanged. Keep libdvdnav/libdvdread installed. See
[third-party notices](THIRD_PARTY_NOTICES.md) for dependency licenses.

Open the intended DVD title in VLC and reopen the extension. Choices show title,
duration, track and language. If no title is available, discovery explicitly uses
the longest title. Jobs and sidecars include the title identity to keep cuts
separate. Sources inside `VIDEO_TS` are never modified.

## Command line

Run from the extracted directory. Inspect **absolute FFprobe stream indices** first
(the extension's displayed track numbers follow VLC instead):

```sh
python3 -m codex_subs scan /path/movie.mkv
python3 -m codex_subs translate \
  --media /path/movie.mkv --track 2 \
  --output /path/movie.ru.srt --workers 15 --fast
```

Or use external multilingual sources:

```sh
python3 -m codex_subs translate \
  --source /path/movie.en.ass \
  --reference /path/movie.fr.srt \
  --output /path/movie.ru.ass
```

The output format must match the extracted source; bitmap sources produce `.ass`.
Use `python3 -m codex_subs translate --help` for all flags, including `--model`,
`--mode`, `--chunk-size`, `--stop-on-uncertain` and executable overrides. A glossary
JSON object can establish names and recurring terms. Reports accompany the output;
source images are kept in `<output>.sources/`. Existing output files are refused.
User-authored ASS karaoke and vector drawings are not accepted as translation input.

## Update, remove, troubleshoot

To update, download a new release and run `python3 install.py` again, then close
and reopen the extension or restart VLC. Already running workers retain the version
they started with. Configuration, saved subtitles and checkpoints are kept.

To remove the extension while keeping generated files:

```sh
python3 install.py --uninstall
```

- **No helper / stale progress:** inspect the job's `launcher.log` and `worker.log`.
  **Cancel translation** also reconciles a stopped helper's stale OCR status.
- **Authentication or unavailable model:** run `codex login` and select an available
  model. Update Codex through the installation method you used originally.
- **429 / rate limit:** let the shared cooldown finish. Fast does not bypass it.
- **Original captions retained:** inspect `originals_kept` and OCR evidence. Clearer
  source subtitles may resolve those captions on a later run.
- **Result not visible:** click **Refresh progress**, or use VLC's **Subtitles → Add
  Subtitle File** on the matching video.

## Development and license

```sh
python3 -m unittest discover -s tests -v
python3 build_release.py
```

Optional `lupa` enables Lua 5.1 callback tests. Tests cover timing, protected
formatting, review coverage, conservative fallback, OCR, DVD decoding, concurrency,
cancellation, recovery, clean installation and source-only release packaging.
CI runs on macOS and Linux. Native OCR/DVD integration needs the local dependencies
above; unit tests use synthetic fixtures without films or model requests.

[MIT](LICENSE). Optional locally built DVD binaries include GPL dependencies;
see [third-party notices](THIRD_PARTY_NOTICES.md). This is an unofficial community
project, not affiliated with OpenAI or VideoLAN.

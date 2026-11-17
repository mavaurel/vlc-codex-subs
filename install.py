#!/usr/bin/env python3
"""Install the user-level VLC extension. No admin rights or Python packages required."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys


def locations():
    if sys.platform == "darwin":
        support = Path.home() / "Library/Application Support"
        return support / "org.videolan.vlc/lua/extensions", support / "VLC Codex Subtitles"
    if sys.platform.startswith("linux"):
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        return base / "vlc/lua/extensions", base / "vlc-codex-subtitles"
    raise SystemExit("The VLC launcher supports macOS and Linux. Windows can use the Python translation CLI.")


def lua_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true", help="Remove the extension; keep jobs/config/subtitles")
    parser.add_argument("--extension-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--codex", help="Explicit Codex executable path (otherwise keep installed setting or use PATH)")
    args = parser.parse_args()
    default_ext, default_data = locations()
    extensions = (args.extension_dir or default_ext).expanduser().resolve()
    data = (args.data_dir or default_data).expanduser().resolve()
    target = extensions / "codex_russian_subtitles.lua"
    if args.uninstall:
        if target.exists() and 'title = "Codex Russian Subtitles"' not in target.read_text(encoding="utf-8"):
            raise SystemExit("Refusing to remove an unrecognized extension.")
        target.unlink(missing_ok=True)
        print(f"Removed extension. Saved subtitles and configuration remain in {data}")
        return
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")
    saved_config = data / "config.json"
    prior = json.loads(saved_config.read_text(encoding="utf-8")) if saved_config.exists() else {}
    if not (codex := shutil.which(args.codex or prior.get("codex", "codex"))):
        raise SystemExit("Codex CLI is missing from PATH. Install it and run codex login, then retry.")
    if target.exists() and 'title = "Codex Russian Subtitles"' not in target.read_text(encoding="utf-8"):
        raise SystemExit("A different file already exists at the extension path; it was not changed.")
    source = Path(__file__).resolve().parent
    app = data / "app"
    for folder in (extensions, data, app, data / "jobs", data / "cache"):
        folder.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    shutil.copytree(source / "codex_subs", app / "codex_subs", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config = data / "config.json"
    settings = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    settings.update({"codex": codex, "ffmpeg": shutil.which("ffmpeg") or "ffmpeg",
                     "ffprobe": shutil.which("ffprobe") or "ffprobe"})
    settings.setdefault("model", "gpt-6.1-sol")
    settings.setdefault("chunk_size", 64)
    settings.setdefault("workers", 4)
    settings.setdefault("service_tier", "")
    settings.setdefault("profile", "light")
    settings.setdefault("keep_original", True)
    settings.setdefault("save_beside_video", True)
    settings.setdefault("timeout", 600)
    settings.setdefault("glossary", {})
    settings["executable_path"] = os.pathsep.join(dict.fromkeys(
        [str(Path(codex).parent), str(Path(sys.executable).parent), "/opt/homebrew/bin",
         "/usr/local/bin", "/usr/bin", "/bin", os.environ.get("PATH", "")]))
    config.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config.chmod(0o600)
    script = (source / "extension.lua").read_text(encoding="utf-8")
    for marker, value in {"PYTHON": sys.executable, "APP": app, "DATA": data, "CONFIG": config}.items():
        script = script.replace(f"@{marker}@", lua_string(value))
    target.write_text(script, encoding="utf-8")
    sys.path.insert(0,str(source))
    from codex_subs.storage import backfill_history
    backfill_history(data)
    print(f"Installed: {target}\nConfiguration and saved subtitles: {data}")
    print("Restart VLC, then open VLC > Extensions > Codex Russian Subtitles on macOS (View menu on Linux).")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("FFmpeg is missing: external files work; embedded tracks require ffmpeg and ffprobe.")


if __name__ == "__main__":
    main()

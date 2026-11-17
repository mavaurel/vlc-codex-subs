#!/bin/bash
# Run from Finder or a terminal; all paths are relative to this download.
set -eu
cd -- "$(dirname -- "$0")"
# Finder does not inherit the shell's Homebrew PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Python 3.10+ is required. Install Python, then run this installer again.'
    exit 1
fi
if ! python3 install.py "$@"; then
    echo 'Installation did not finish. See the message above and README.ru.md / README.md.'
    if [[ -t 0 && $# -eq 0 ]]; then read -r -p 'Press Return to close…' _; fi
    exit 1
fi
if [[ -t 0 && $# -eq 0 ]]; then read -r -p 'Press Return to close…' _; fi

# Third-party software

The Python, Lua, Swift, build scripts and native wrapper source in this repository
are provided under the MIT license in `LICENSE`.

VLC, Codex CLI, FFmpeg, Apple frameworks, libdvdnav and libdvdread are separate
software dependencies. This project is not affiliated with VideoLAN or OpenAI.
No films, subtitle files, credentials or dependency binaries are distributed in
this repository or its installation archive.

## Optional DVD reader

`setup_dvd.py` downloads official FFmpeg source, checks its SHA-256 and applies
the two-site signed DVD timestamp correction recorded in that script. It then
links the local native wrapper with an FFmpeg GPL-enabled build and Homebrew's
libdvdnav/libdvdread. The resulting helper is built on the user's machine;
it is excluded from the public source and installation ZIP.

- FFmpeg upstream and source version/checksum: `setup_dvd.py` and
  <https://ffmpeg.org/download.html>
- FFmpeg licensing: <https://ffmpeg.org/legal.html>
- libdvdnav and libdvdread: <https://www.videolan.org/developers/libdvdnav.html>

The generated helper is subject to the licenses of those linked components,
including the GNU GPL; it is not a standalone MIT binary. The build keeps
`COPYING.FFmpeg.GPLv2` and an `FFmpeg-SOURCE.txt` record beside it. Anyone
redistributing that helper must also meet the applicable source and notice
requirements. The supplied release builder deliberately packages source only.

"""Local DVD folders: navigate titles, decode bitmap subtitles, reuse local OCR."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .pgs import Bitmap, Display, PGSError, png
from .process import Cancelled, run


def folder(path):
    path = Path(path)
    if path.name.upper() == 'VIDEO_TS' and (path/'VIDEO_TS.IFO').is_file():
        return path.parent
    if (path/'VIDEO_TS'/'VIDEO_TS.IFO').is_file():
        return path
    raise ValueError('Choose a local DVD folder containing VIDEO_TS/VIDEO_TS.IFO.')


def requested_title(uri):
    if not uri.startswith(('dvdnav:', 'dvd:')):
        return None
    fragment = urlsplit(uri).fragment
    match = re.fullmatch(r'(?:codex-title=)?(\d+)(?::\d*){0,2}', fragment)
    if fragment and not match:
        raise ValueError('Unsupported DVD title selector.')
    value = int(match[1]) if match else 0
    if value > 99:
        raise ValueError('Invalid DVD title selector.')
    return value or None  # DVD menu title 0: offer the main (longest) title.


def helper(config=None):
    path = Path((config or {}).get('dvd_helper') or Path(__file__).with_name('bin')/'dvd-subtitles')
    if not path.is_file():
        raise RuntimeError('DVD support needs its local reader. Run python3 setup_dvd.py, then install.py.')
    return path


def scan(path, config=None, cancel=None):
    with tempfile.TemporaryDirectory(prefix='codex-dvd-scan-') as temporary:
        target = Path(temporary)/'scan.json'
        run([str(helper(config)), 'scan', str(folder(path)), str(target)], timeout=120, cancel=cancel)
        result = json.loads(target.read_text())
    titles = [t for t in result['titles'] if not t.get('error')]
    if not titles:
        raise ValueError('No readable DVD titles found.')
    return titles


def select_title(titles, title=None):
    if title is not None:
        selected = next((t for t in titles if t['title'] == title), None)
        if selected is None:
            raise ValueError(f'DVD title {title} is not readable.')
        return selected
    return max(titles, key=lambda t: t['duration'])


def discover(path, config=None, cancel=None, title=None):
    from .media import LANGUAGES
    chosen = select_title(scan(path, config, cancel), title)
    minutes, seconds = divmod(round(chosen['duration']), 60)
    choices = []
    for number, stream in enumerate(chosen['streams'], 1):
        language = LANGUAGES.get(stream['language'], stream['language'])
        choices.append({'kind':'dvd_track', 'value':f"{chosen['title']}:{stream['index']}",
                        'label':f"DVD title {chosen['title']} ({minutes}:{seconds:02d}), track {number}: {language} — DVD (OCR)"})
    warnings = [] if title else [f"DVD title was not specified; showing the longest title ({chosen['title']}). Start the intended film in VLC and retry detection to follow its title."]
    return choices, warnings


def displays(path, cancel=None):
    """Read a completed native extraction, rejecting partial manifests or bad images."""
    root = path.parent.resolve()
    data = json.loads(path.read_text())
    width, height = data['width'], data['height']
    if data.get('version') != 1 or not (0 < width <= 4096 and 0 < height <= 2160):
        raise PGSError('Invalid DVD bitmap manifest.')
    previous = -1
    for item in data['displays']:
        if cancel and cancel.exists():
            raise Cancelled('Cancelled during DVD decoding.')
        start, end = item['start'], item['end']
        if not (0 <= previous <= start < end) and not (previous == -1 and 0 <= start < end):
            raise PGSError('Invalid DVD subtitle timing.')
        previous = start
        parts = []
        for part in item['parts']:
            x,y,w,h = (part[k] for k in ('x','y','width','height'))
            image = (root/part['file']).resolve()
            if image.parent != root or not (0 <= x < x+w <= width and 0 <= y < y+h <= height):
                raise PGSError('Invalid DVD subtitle image bounds.')
            if image.stat().st_size != w*h:
                raise PGSError('Incomplete DVD subtitle image.')
            pixels = image.read_bytes()
            if min(pixels) == 255:
                raise PGSError('DVD subtitle image contains no visible text.')
            parts.append(Bitmap(x,y,w,h,png(w,h,pixels),bool(part.get('forced'))))
        if not parts:
            raise PGSError('DVD subtitle has no bitmap.')
        yield Display(start,end,width,height,tuple(parts))


def extract(path, value, root, config, cancel=None, progress=lambda *args:None, verifier=None):
    from .ocr import convert
    if not re.fullmatch(r'[1-9]\d?:\d{1,3}', str(value)):
        raise ValueError('Invalid DVD subtitle selection.')
    title, index = map(int, value.split(':'))
    chosen = select_title(scan(path, config, cancel), title)
    stream = next((s for s in chosen['streams'] if s['index']==index), None)
    if stream is None:
        raise ValueError('The selected DVD subtitle track no longer exists.')
    source = Path(root)/f'dvd-title-{title}-track-{index}'
    source.mkdir(parents=True, exist_ok=True)
    raw = source/'bitmaps'
    raw.mkdir(exist_ok=True)
    manifest = raw/'displays.json'
    # Only completed extractions may be reused after cancellation/restart.
    complete = raw/'complete'
    reader = helper(config)
    video_ts = folder(path)/'VIDEO_TS'
    identity = json.dumps({'reader':hashlib.sha256(reader.read_bytes()).hexdigest(),
                           'ifo':[(p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(video_ts.glob('*.IFO'))]},sort_keys=True)
    if not complete.exists() or complete.read_text() != identity:
        progress('extracting',0,0,f'Reading DVD title {title}, track {index}; preserving chapter timing...')
        complete.unlink(missing_ok=True)
        run([str(reader), 'extract', str(folder(path)), str(title), str(index), str(raw)],
            timeout=1800, cancel=cancel)
        # Validate the whole stream before marking it reusable.
        count = sum(1 for _ in displays(manifest, cancel))
        if not count:
            raise PGSError('No visible DVD subtitles found.')
        complete.write_text(identity)
    language = stream['language']
    return convert(manifest, source, cache=Path(config.get('ocr_cache',Path(root)/'cache')),
                   language=config.get('ocr_language','') if language=='und' else language,
                   cancel=cancel, progress=progress, verifier=verifier,
                   keep_original=config.get('keep_original',True), display_reader=displays, source_format='DVD')

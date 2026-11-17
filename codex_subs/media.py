from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .process import Cancelled, run
from .subtitles import read, SubtitleError


TEXT_CODECS = {"ass": ".ass", "ssa": ".ssa", "subrip": ".srt",
               "srt": ".srt", "webvtt": ".vtt", "mov_text": ".srt", "text": ".srt"}
EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sup"}
LANGUAGES = {"en": "English", "eng": "English", "ru": "Russian", "rus": "Russian",
             "dut":"Dutch", "nld":"Dutch", "swe":"Swedish", "fin":"Finnish", "nor":"Norwegian", "dan":"Danish",
             "gre":"Greek", "ell":"Greek", "hun":"Hungarian", "slv":"Slovenian", "hrv":"Croatian", "bul":"Bulgarian",
             "fr": "French", "fra": "French", "fre": "French", "es": "Spanish", "spa": "Spanish",
             "de": "German", "deu": "German", "ger": "German", "it": "Italian", "ita": "Italian",
             "pt": "Portuguese", "por": "Portuguese", "ja": "Japanese", "jpn": "Japanese",
             "zh": "Chinese", "zho": "Chinese", "chi": "Chinese", "ko": "Korean", "kor": "Korean",
             "uk": "Ukrainian", "ukr": "Ukrainian", "ar": "Arabic", "ara": "Arabic",
             "pl": "Polish", "pol": "Polish", "tr": "Turkish", "tur": "Turkish",
             "und": "Unlabelled subtitles"}


def local_path(value: str) -> Path:
    dvd = value.startswith(('dvdnav:', 'dvd:'))
    if value.startswith("file:") or dvd:
        url = urlsplit(value)
        if url.netloc not in ("", "localhost"):
            raise ValueError("Only local media files are supported.")
        value = unquote(url.path)
    elif "://" in value:
        raise ValueError("Open a local media file; streaming URLs are not supported.")
    path = Path(value).expanduser().resolve(strict=True)
    if dvd or path.is_dir():
        from .dvd import folder
        return folder(path)
    if not path.is_file():
        raise ValueError("Expected a local file.")
    return path


def probe(path: Path, ffprobe="ffprobe", cancel=None) -> list[dict]:
    result = run([ffprobe, "-v", "error", "-select_streams", "s", "-show_streams",
                  "-of", "json", str(path)], timeout=45, cancel=cancel)
    return json.loads(result).get("streams", [])


def discover(path: Path, ffprobe="ffprobe", cancel=None, *, dvd_config=None, dvd_title=None) -> tuple[list[dict], list[str]]:
    if path.is_dir():
        from .dvd import discover as discover_dvd
        return discover_dvd(path, dvd_config, cancel, dvd_title)
    choices, warnings = [], []
    for candidate in sorted(path.parent.iterdir()):
        if (candidate.is_file() and candidate.suffix.lower() in EXTENSIONS
                and (candidate.stem == path.stem or candidate.stem.startswith(path.stem + "."))):
            try:
                if candidate.suffix.lower() == '.sup':
                    with candidate.open('rb') as f:
                        if f.read(2) != b'PG':
                            raise SubtitleError('Not a PGS stream')
                else:
                    read(candidate)
            except (OSError, UnicodeError, SubtitleError) as exc:
                warnings.append(f"Skipped {candidate.name}: not a readable text subtitle ({exc}).")
                continue
            choices.append({"kind": "file", "value": str(candidate), "label": candidate.name})
    try:
        streams = probe(path, ffprobe, cancel)
    except Cancelled:
        raise
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        warnings.append(f"Embedded scan unavailable: {exc}")
        streams = []
    embedded = []
    counts = {s['index']: int(s.get('tags',{}).get('NUMBER_OF_FRAMES','0'))
              for s in streams if s.get('tags',{}).get('NUMBER_OF_FRAMES','0').isdigit()}
    largest = max(counts.values(), default=0)
    short = {i for i,n in counts.items() if n > 0 and largest > 100 and n < largest/10}
    track_numbers = {s['index']: i+1 for i,s in enumerate(streams)}
    for stream in sorted(streams, key=lambda s: (s.get('codec_name') not in TEXT_CODECS,
                                               bool(s.get("disposition", {}).get("forced")), s['index'] in short,
                                               not bool(s.get("disposition", {}).get("default")), s["index"])):
        tags = stream.get("tags", {})
        codec = stream.get("codec_name", "unknown")
        language = tags.get("language", "und").lower()
        human = LANGUAGES.get(language, language)
        title = tags.get("title", "")
        name = title if title.lower().startswith(human.lower()) else f"{human}: {title}" if title else human
        pgs = codec == 'hdmv_pgs_subtitle'
        label = f"Track {track_numbers[stream['index']]}: {name} — " + ('PGS (OCR)' if pgs else 'embedded')
        if stream['index'] in short: label += ', short track'
        if codec not in TEXT_CODECS and not pgs:
            warnings.append(f"{label}: unsupported subtitle format {codec}.")
            continue
        if pgs and sys.platform != 'darwin':
            warnings.append(f"{label}: PGS OCR currently requires macOS.")
            continue
        embedded.append({"kind": "track", "value": str(stream["index"]), "label": label})
    return embedded + choices, warnings


def extract(media: Path, index: int, directory: Path, ffmpeg="ffmpeg", ffprobe="ffprobe", cancel=None,
            *, cache=None, progress=lambda *args:None, ocr_language='', verifier=None, keep_original=False) -> Path:
    directory.mkdir(parents=True,exist_ok=True)
    stream = next((s for s in probe(media, ffprobe, cancel) if s["index"] == index), None)
    if stream is None:
        raise ValueError(f"Subtitle stream {index} does not exist.")
    codec = stream.get("codec_name")
    if codec == 'hdmv_pgs_subtitle':
        from .ocr import convert
        from .pgs import extract_sup
        progress('extracting',0,0,'Extracting the PGS subtitle images...')
        target = directory / f'track-{index}.sup'
        directory.mkdir(parents=True,exist_ok=True)
        extract_sup(media,index,target,ffmpeg,ffprobe,cancel)
        language = stream.get('tags',{}).get('language','und')
        return convert(target,directory/f'pgs-track-{index}',cache=cache or directory/'cache',
                       language=ocr_language if language == 'und' else language,cancel=cancel,progress=progress,
                       verifier=verifier,keep_original=keep_original)
    if codec not in TEXT_CODECS:
        raise ValueError("Unsupported subtitle format. Supply text subtitles or a PGS track.")
    target = directory / f"track-{index}{TEXT_CODECS[codec]}"
    output_codec = "copy" if codec in ("ass", "ssa", "subrip", "srt", "webvtt") else "srt"
    run([ffmpeg, "-nostdin", "-v", "error", "-y", "-copyts", "-i", str(media),
         "-map", f"0:{index}", "-c:s", output_codec, str(target)], timeout=120, cancel=cancel)
    return target

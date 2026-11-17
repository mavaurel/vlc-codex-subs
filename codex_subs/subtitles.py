"""Lossless subtitle skeletons: only dialogue spans can be replaced."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import bisect
import re


class SubtitleError(ValueError):
    pass


TOKEN = re.compile(r"⟦KEEP_\d+⟧")
MARKUP = re.compile(r"\{[^{}\r\n]*\}|<[^<>\r\n]+>|\\[Nnh]|\r\n|\r|\n|[♪♫]+")
SRT_TIME = re.compile(r"^(\d{1,3}:\d{2}:\d{2}[,.]\d{3})[ \t]+-->[ \t]+(\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?:[ \t].*)?$")
VTT_TIME = re.compile(r"^((?:\d{2,}:)?\d{2}:\d{2}\.\d{3})[ \t]+-->[ \t]+((?:\d{2,}:)?\d{2}:\d{2}\.\d{3})(?:[ \t].*)?$")


def seconds(value: str) -> float:
    parts = value.strip().replace(",", ".").split(":")
    if len(parts) not in (2, 3):
        raise SubtitleError(f"Invalid timestamp: {value}")
    try:
        return sum(float(v) * 60 ** i for i, v in enumerate(reversed(parts)))
    except ValueError as exc:
        raise SubtitleError(f"Invalid timestamp: {value}") from exc


@dataclass(frozen=True)
class Cue:
    id: int
    start: float
    end: float
    begin: int
    finish: int
    text: str
    speaker: str = ""

    def protected(self) -> tuple[str, list[str]]:
        if "⟦KEEP_" in self.text:
            raise SubtitleError("Source contains a reserved formatting marker.")
        tokens: list[str] = []

        def replace(match):
            tokens.append(match.group())
            return f"⟦KEEP_{len(tokens) - 1}⟧"

        return MARKUP.sub(replace, self.text), tokens

    def restore(self, translated: str) -> str:
        source, protected = self.protected()
        expected = [f"⟦KEEP_{i}⟧" for i in range(len(protected))]
        if TOKEN.findall(translated) != expected:
            raise SubtitleError(f"Cue {self.id}: formatting markers were changed or reordered.")
        plain = TOKEN.sub("", translated)
        if "⟦KEEP_" in plain or MARKUP.search(plain) or "-->" in plain:
            raise SubtitleError(f"Cue {self.id}: new formatting or a subtitle boundary was added.")
        if any(ord(c) < 32 for c in plain) or any(c in plain for c in "\u2028\u2029"):
            raise SubtitleError(f"Cue {self.id}: unexpected control character.")
        original_parts = TOKEN.split(source)
        translated_parts = TOKEN.split(translated)
        for before, after in zip(original_parts, translated_parts):
            if before.strip() and not after.strip():
                raise SubtitleError(f"Cue {self.id}: dialogue segment was dropped.")
            if not before.strip() and after.strip():
                raise SubtitleError(f"Cue {self.id}: text was added outside a dialogue segment.")
        if len(plain) > max(100, len(TOKEN.sub("", source)) * 4):
            raise SubtitleError(f"Cue {self.id}: suspiciously long translation; review needed.")
        return TOKEN.sub(lambda m: protected[int(m.group()[6:-1])], translated)


@dataclass
class Document:
    text: str
    cues: list[Cue]
    suffix: str
    bom: bool = False

    def render(self, translations: dict[int, str]) -> bytes:
        if set(translations) != {c.id for c in self.cues}:
            raise SubtitleError("Cue IDs are missing, duplicated, or unexpected.")
        chunks, offset = [], 0
        for cue in self.cues:
            chunks += [self.text[offset:cue.begin], cue.restore(translations[cue.id])]
            offset = cue.finish
        chunks.append(self.text[offset:])
        return (("\ufeff" if self.bom else "") + "".join(chunks)).encode("utf-8")


def parse(text: str, suffix: str, *, allow_drawings=False) -> Document:
    suffix = suffix.lower()
    bom = text.startswith("\ufeff")
    text = text.removeprefix("\ufeff")
    cues: list[Cue] = []

    def add(start, end, begin, finish, speaker=""):
        if end <= start or start < 0:
            raise SubtitleError("A cue has an invalid time range.")
        raw = text[begin:finish]
        # Vector drawings and karaoke syllable timing cannot be faithfully translated.
        if not allow_drawings and suffix in (".ass", ".ssa") and re.search(r"\\(?:p[1-9]\d*|[kK](?:[fo])?\d+)", raw):
            raise SubtitleError("ASS drawings/karaoke need manual handling; choose a dialogue-only track.")
        if not raw.strip():
            raise SubtitleError("Empty subtitle cue.")
        cues.append(Cue(len(cues) + 1, start, end, begin, finish, raw, speaker))

    if suffix in (".srt", ".vtt"):
        if suffix == ".vtt" and not text.startswith("WEBVTT"):
            raise SubtitleError("Missing WEBVTT header.")
        blocks = re.finditer(r"[^\r\n]+(?:\r?\n[^\r\n]+)*", text)
        for block in blocks:
            lines = list(re.finditer(r"[^\r\n]+", block.group()))
            first = lines[0].group()
            if suffix == ".vtt" and (first.startswith("WEBVTT") or re.match(r"^(NOTE(?:\s|$)|STYLE$|REGION$)", first)):
                continue
            pattern = SRT_TIME if suffix == ".srt" else VTT_TIME
            ti = 1 if suffix == ".srt" else (0 if pattern.fullmatch(first) else 1)
            if suffix == ".srt" and not first.strip().isdigit():
                raise SubtitleError("Malformed SRT: expected a numeric cue identifier.")
            if len(lines) <= ti + 1 or not (match := pattern.fullmatch(lines[ti].group())):
                raise SubtitleError(f"Malformed subtitle block near: {first[:70]}")
            add(seconds(match[1]), seconds(match[2]), block.start() + lines[ti + 1].start(), block.end())
    elif suffix in (".ass", ".ssa"):
        section, fields = "", []
        for line in re.finditer(r"[^\r\n]+", text):
            raw = line.group()
            if raw.strip().startswith("["):
                section = raw.strip().lower()
                fields = []
            elif section == "[events]":
                if raw.lower().startswith("format:"):
                    fields = [v.strip().lower() for v in raw.split(":", 1)[1].split(",")]
                    if not fields or fields[-1] != "text" or not {"start", "end"} <= set(fields):
                        raise SubtitleError("Unsupported ASS event field layout (Text must be last).")
                elif raw.lower().startswith("dialogue:"):
                    if not fields:
                        raise SubtitleError("ASS Dialogue appears before its Format declaration.")
                    body_start = raw.index(":") + 1
                    values = raw[body_start:].split(",", len(fields) - 1)
                    if len(values) != len(fields):
                        raise SubtitleError("Malformed ASS Dialogue.")
                    begin = line.start() + body_start + sum(len(v) + 1 for v in values[:-1])
                    speaker = values[fields.index("name")].strip() if "name" in fields else ""
                    add(seconds(values[fields.index("start")]), seconds(values[fields.index("end")]), begin, line.end(), speaker)
    else:
        raise SubtitleError("Supported text formats: .srt, .ass, .ssa, .vtt")
    if not cues:
        raise SubtitleError("No dialogue cues found.")
    return Document(text, cues, suffix, bom)


def read(path: Path, encoding="utf-8-sig") -> Document:
    if path.stat().st_size > 25_000_000:
        raise SubtitleError("Subtitle file exceeds 25 MB.")
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise SubtitleError("Subtitle encoding is not UTF-8/UTF-16. Convert it or use --encoding.") from exc
    doc = parse(text, path.suffix)
    doc.bom = data.startswith(b"\xef\xbb\xbf")
    return doc


def align(primary: Document, reference: Document) -> dict[int, list[Cue]]:
    """Use temporal overlap; never align by cue number or invent missing cues."""
    ordered = sorted(reference.cues, key=lambda c: c.start)
    starts = [c.start for c in ordered]
    max_duration = max(c.end - c.start for c in ordered)
    result = {}
    for cue in primary.cues:
        lo = bisect.bisect_left(starts, cue.start - max_duration)
        hi = bisect.bisect_left(starts, cue.end)
        hits = [r for r in ordered[lo:hi]
                if min(cue.end, r.end) - max(cue.start, r.start)
                >= 0.35 * min(cue.end - cue.start, r.end - r.start)]
        result[cue.id] = hits
    return result

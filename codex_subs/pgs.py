"""Strict, streaming PGS/SUP display-set reader. No OCR or guessed end times here."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import struct
import zlib
import json
from fractions import Fraction

from .process import Cancelled, run


class PGSError(ValueError):
    pass


def extract_sup(media, index, target, ffmpeg, ffprobe, cancel=None):
    """Preserve packet PTS even in MKVs with out-of-order subtitle packets.

    FFmpeg's SUP muxer clamps nonmonotonic DTS (and PTS), changing cue times.
    Raw stream copy plus the original packet table avoids that timestamp rewrite.
    """
    packets = json.loads(run([ffprobe,'-v','error','-select_streams',str(index),'-show_packets',
        '-show_entries','packet=pts_time,size','-of','json',str(media)],timeout=180,cancel=cancel)).get('packets',[])
    raw = target.with_suffix('.raw')
    try:
        run([ffmpeg,'-nostdin','-v','error','-y','-i',str(media),'-map',f'0:{index}',
             '-c','copy','-f','data',str(raw)],timeout=180,cancel=cancel)
        offsets, offset = [],0
        for packet in packets:
            if 'pts_time' not in packet:
                raise PGSError('PGS packet has no presentation time.')
            ticks = round(Fraction(packet['pts_time'])*90000)
            size = int(packet['size'])
            if not 0 <= ticks < 2**32 or size < 1:
                raise PGSError('Unsupported PGS packet timestamp or size.')
            offsets.append((ticks,offset,size)); offset += size
        if offset != raw.stat().st_size or offset > 256_000_000:
            raise PGSError('PGS packet table does not match the extracted stream.')
        with raw.open('rb') as source, target.open('wb') as out:
            for ticks,offset,size in sorted(offsets):
                if cancel and cancel.exists(): raise Cancelled('Cancelled during extraction.')
                source.seek(offset)
                packet = Reader(source.read(size))
                while packet.at < size:
                    kind,length = packet.number(1),packet.number(2)
                    body = packet.take(length)
                    out.write(struct.pack('>2sIIBH',b'PG',ticks,ticks,kind,length)+body)
    finally:
        raw.unlink(missing_ok=True)


class Reader:
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, n):
        if n < 0 or self.at + n > len(self.data):
            raise PGSError("Truncated PGS segment.")
        value = self.data[self.at:self.at+n]
        self.at += n
        return value

    def number(self, n):
        return int.from_bytes(self.take(n), "big")

    def rest(self):
        return self.take(len(self.data)-self.at)


def dimensions(w, h):
    if not 0 < w <= 8192 or not 0 < h <= 8192 or w*h > 16_777_216:
        raise PGSError("Invalid or excessive PGS image dimensions.")


def decode_rle(data: bytes, width: int, height: int) -> bytes:
    dimensions(width, height)
    source, out, column, row = Reader(data), bytearray(), 0, 0
    while source.at < len(data):
        if row >= height:
            raise PGSError("Extra PGS image data.")
        color, length = source.number(1), 1
        if color == 0:
            flags = source.number(1)
            if flags == 0:
                if column != width:
                    raise PGSError("PGS row has missing pixels.")
                row, column = row+1, 0
                continue
            length = flags & 63
            if flags & 64:
                length = length*256 + source.number(1)
            color = source.number(1) if flags & 128 else 0
        if length == 0 or column+length > width:
            raise PGSError("Invalid PGS pixel run.")
        out.extend(bytes([color])*length)
        column += length
    if len(out) != width*height or (row != height and not (row == height-1 and column == width)):
        raise PGSError("Incomplete PGS image.")
    return bytes(out)


def png(width, height, pixels):
    """8-bit grayscale PNG, including a quiet border for the OCR engine."""
    border = 16
    w, h = width+2*border, height+2*border
    blank = b'\x00' + b'\xff'*w
    rows = [blank]*border
    rows += [b'\x00' + b'\xff'*border + pixels[y*width:(y+1)*width] + b'\xff'*border for y in range(height)]
    rows += [blank]*border
    def chunk(kind, data):
        return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w,h,8,0,0,0,0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))


@dataclass(frozen=True)
class Bitmap:
    x: int
    y: int
    width: int
    height: int
    image: bytes
    forced: bool = False


@dataclass(frozen=True)
class Display:
    start: int  # 90 kHz ticks, original video timeline
    end: int
    width: int
    height: int
    parts: tuple[Bitmap, ...]


def displays(path: Path, cancel=None):
    """Yield fully delimited displays. Reject corrupt images instead of losing dialogue."""
    if path.stat().st_size > 256_000_000:
        raise PGSError("PGS file exceeds 256 MB.")
    objects, palettes, windows = {}, {}, {}
    pending, active, previous_pts, wrap = None, None, None, 0
    with path.open('rb') as stream:
        while header := stream.read(13):
            if cancel and cancel.exists():
                raise Cancelled("Cancelled during PGS decoding.")
            if len(header) != 13 or header[:2] != b'PG':
                raise PGSError("Not a valid PGS .sup stream.")
            _, pts, _, kind, length = struct.unpack('>2sIIBH', header)
            body = stream.read(length)
            if len(body) != length:
                raise PGSError("Truncated PGS segment.")
            s = Reader(body)
            if kind == 0x16:
                if pending is not None:
                    raise PGSError("PGS display is missing its END segment.")
                if previous_pts is not None and pts < previous_pts and previous_pts-pts > 2**31:
                    wrap += 2**32
                elif previous_pts is not None and pts < previous_pts:
                    raise PGSError("PGS presentation times moved backwards.")
                previous_pts = pts
                w,h = s.number(2),s.number(2)
                dimensions(w,h)
                s.take(3)  # frame rate, composition number
                state = s.number(1)
                s.take(1)  # palette update flag
                palette_id,count = s.number(1),s.number(1)
                if count > 2:
                    raise PGSError("Unsupported PGS display with more than two objects.")
                if state & 0xc0:
                    objects,palettes,windows = {},{},{}
                refs = []
                for _ in range(count):
                    oid,window,flags,x,y = s.number(2),s.number(1),s.number(1),s.number(2),s.number(2)
                    crop = tuple(s.number(2) for _ in range(4)) if flags & 128 else None
                    refs.append((oid,window,flags,x,y,crop))
                pending = (pts+wrap,w,h,palette_id,refs)
            elif kind == 0x14:
                pid = s.number(1)
                s.take(1)
                palette = palettes.setdefault(pid, {0:(16,128,128,0)})
                while s.at < length:
                    index = s.number(1)
                    palette[index] = tuple(s.number(1) for _ in range(4))
                if len(palettes) > 8:
                    raise PGSError("Too many PGS palettes.")
            elif kind == 0x15:
                oid,version,flags = s.number(2),s.number(1),s.number(1)
                if flags & 128:
                    size,w,h = s.number(3)-4,s.number(2),s.number(2)
                    dimensions(w,h)
                    if size < 1 or size > 64_000_000:
                        raise PGSError("Invalid PGS object size.")
                    objects[oid] = [version,size,w,h,bytearray(),False]
                if oid not in objects or objects[oid][0] != version:
                    raise PGSError("Missing PGS object fragment.")
                obj = objects[oid]
                obj[4].extend(s.rest())
                obj[5] = bool(flags & 64)
                if len(obj[4]) > obj[1] or len(objects) > 64 or sum(len(o[4]) for o in objects.values()) > 64_000_000:
                    raise PGSError("Excessive PGS object data.")
            elif kind == 0x17:
                for _ in range(s.number(1)):
                    wid = s.number(1)
                    windows[wid] = tuple(s.number(2) for _ in range(4))
            elif kind == 0x80:
                if pending is None:
                    raise PGSError("PGS END without a presentation.")
                timestamp,w,h,pid,refs = pending
                pending,parts = None,[]
                for oid,window,flags,x,y,crop in refs:
                    if oid not in objects or pid not in palettes:
                        raise PGSError("PGS display is missing an object or palette.")
                    _,size,ow,oh,rle,complete = objects[oid]
                    if not complete or len(rle) != size:
                        raise PGSError("Incomplete PGS object fragments.")
                    indices = decode_rle(rle,ow,oh)
                    palette = palettes[pid]
                    if set(indices)-palette.keys():
                        raise PGSError("PGS image uses an undefined palette entry.")
                    # Composite luminance on black, then invert to dark ink on white.
                    table = bytes(255-round(max(0,min(255,(palette.get(i,(16,128,128,0))[0]-16)*255/219))
                                          *palette.get(i,(16,128,128,0))[3]/255) for i in range(256))
                    image = indices.translate(table)
                    cx,cy,cw,ch = crop or (0,0,ow,oh)
                    if cx+cw > ow or cy+ch > oh or cw <= 0 or ch <= 0:
                        raise PGSError("Invalid PGS object crop.")
                    wx,wy,ww,wh = windows.get(window,(0,0,w,h))
                    left,top = max(x,wx,0),max(y,wy,0)
                    right,bottom = min(x+cw,wx+ww,w),min(y+ch,wy+wh,h)
                    if right <= left or bottom <= top:
                        raise PGSError("PGS object is outside its window.")
                    cx,cy = cx+left-x,cy+top-y
                    bw,bh = right-left,bottom-top
                    cropped = b''.join(image[(cy+r)*ow+cx:(cy+r)*ow+cx+bw] for r in range(bh))
                    if min(cropped) == 255:  # wholly transparent palette update = clear
                        continue
                    parts.append(Bitmap(left,top,bw,bh,png(bw,bh,cropped),bool(flags & 64)))
                parts = tuple(parts)
                if active and active[1:] == (w,h,parts):
                    continue  # unchanged display; do not split a sentence
                if active:
                    start,aw,ah,aparts = active
                    if timestamp == start and not parts:
                        # Matroska can mux the old cue's clear after the new cue
                        # at the same timestamp. It must not erase the new text.
                        continue
                    if timestamp <= start:
                        raise PGSError("PGS display has no positive duration.")
                    yield Display(start,timestamp,aw,ah,aparts)
                active = (timestamp,w,h,parts) if parts else None
            else:
                raise PGSError(f"Unsupported PGS segment: {kind:#x}.")
    if pending is not None or active is not None:
        raise PGSError("PGS track ends without a clear/end time; refusing to invent timing.")

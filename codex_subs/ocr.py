"""PGS -> local OCR, image verification and faithful original-image fallback."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import copy
import sys
import tempfile

from .pgs import displays, PGSError
from .pipeline import atomic_json, publish, ReviewRequired
from .process import run, Cancelled
from .subtitles import parse

VERSION = 'pgs-vision-1'
MIN_CONFIDENCE = 0.80
LANGUAGES = {'eng':'en','rus':'ru','fra':'fr','fre':'fr','deu':'de','ger':'de','spa':'es',
             'ita':'it','por':'pt','jpn':'ja','kor':'ko','zho':'zh','chi':'zh','ukr':'uk',
             'ara':'ar','pol':'pl','tur':'tr','nld':'nl','dut':'nl','swe':'sv','dan':'da',
             'nor':'no','fin':'fi','ces':'cs','cze':'cs','und':''}


def engine(cache: Path, cancel=None) -> Path:
    if sys.platform != 'darwin':
        raise RuntimeError('PGS OCR currently requires macOS. Supply a text subtitle on this platform.')
    source = Path(__file__).with_name('vision_ocr.swift')
    key = hashlib.sha256(source.read_bytes() + platform.machine().encode()).hexdigest()[:16]
    binary = cache / 'bin' / ('vision-ocr-' + key)
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.building-', dir=binary.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            run(['/usr/bin/xcrun','swiftc','-O',str(source),'-o',str(temporary)],timeout=180,cancel=cancel)
            os.replace(temporary,binary)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError('PGS OCR helper could not be built. Install Apple Command Line Tools with '
                               f'xcode-select --install, then retry. Details: {exc}') from exc
        finally:
            temporary.unlink(missing_ok=True)
    return binary


def ass_time(ticks):
    # ASS has centisecond precision; retain the PGS timeline to nearest 10 ms.
    cs = (ticks+450)//900
    return f'{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}'


def validate_result(result):
    if result.get('error'):
        return str(result['error'])
    lines = result.get('lines',[])
    if not lines:
        return 'No text recognized in a visible subtitle image.'
    for line in lines:
        text = line.get('text','')
        if not isinstance(text,str) or not text.strip():
            return 'Empty OCR line.'
        if any(c in text for c in '{}\\\r\n<>') or any(ord(c)<32 for c in text):
            return 'OCR contains markup or control characters; check the original image.'
        confidence = line.get('confidence',0)
        if not isinstance(confidence,(int,float)) or not math.isfinite(confidence) or (confidence < MIN_CONFIDENCE and not result.get('image_verified')):
            return 'OCR confidence below 80%; check the original image.'
        for name in ('x','y','width','height'):
            value = line.get(name)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= 1:
                return 'Invalid OCR text position.'
    return ''


def suspicious(result):
    if validate_result(result):return True
    return not result.get('image_verified') and any(re.fullmatch(r'[1Il0O][\s.…!?]*',x['text'].strip()) for x in result['lines'])


def verify_image(result,path,verifier):
    """Two fresh image-only readings must agree. Context may not invent glyphs."""
    readings=[verifier.read_image(path),verifier.read_image(path)]
    for r in readings:
        if not isinstance(r,dict) or r.get('uncertain') is not False or not isinstance(r.get('lines'),list) or not r['lines']:
            return None,readings
        if any(not isinstance(s,str) or not s.strip() for s in r['lines']):return None,readings
    a=[s.strip() for s in readings[0]['lines']];b=[s.strip() for s in readings[1]['lines']]
    if a!=b:return None,readings
    fixed=copy.deepcopy(result);old=fixed.get('lines',[]);fixed.pop('error',None)
    fixed['lines']=[{**(old[i] if len(old)==len(a) else {'x':.05,'y':i/len(a),'width':.9,'height':.8/len(a),'confidence':0}), 'text':s} for i,s in enumerate(a)]
    fixed['image_verified']=True;fixed['image_readings']=readings;fixed['original_ocr_lines']=result.get('lines',[])
    return (fixed if not validate_result(fixed) else None),readings


def convert(source: Path, root: Path, *, cache: Path, language='', cancel=None,
            progress=lambda *args:None, verifier=None, keep_original=False, display_reader=None, source_format='PGS') -> Path:
    """Reports/images remain on disk even when OCR needs correction."""
    root.mkdir(parents=True,exist_ok=True)
    language = LANGUAGES.get(language.lower(),language.lower())
    original_progress = progress
    # Keep one rendering/verification path for both bitmap formats.
    progress = lambda state,done,total,message: original_progress(state,done,total,message.replace('PGS',source_format))
    progress('ocr',0,0,'Preparing local OCR for PGS subtitles...')
    binary = engine(cache,cancel)
    images = root/'images'; images.mkdir(exist_ok=True)
    items,inputs = [],{}
    canvas = None
    for display in (display_reader or displays)(source,cancel):
        if canvas is not None and canvas != (display.width,display.height):
            raise PGSError('PGS resolution changes within the track; manual conversion is required.')
        canvas = (display.width,display.height)
        for part in display.parts:
            digest = hashlib.sha256(part.image).hexdigest()
            path = images/(digest+'.png')
            if not path.exists(): path.write_bytes(part.image)
            inputs[digest] = {'id':digest,'path':str(path.resolve()),'language':language}
            items.append({'id':len(items)+1,'start_ticks':display.start,'end_ticks':display.end,
                          'x':part.x,'y':part.y,'width':part.width,'height':part.height,
                          'image':str(path.resolve()),'image_key':digest,'forced':part.forced})
    if not items:
        raise PGSError('No visible PGS subtitles found.')
    report = root/'ocr.json'
    audit = {'version':VERSION,'source':str(source),'source_format':source_format,'language':language or 'auto',
             'engine':'Apple Vision accurate; language correction disabled',
             'minimum_confidence':MIN_CONFIDENCE,'status':'recognizing','cues':items,
             'limitations':'OCR confidence is not a correctness guarantee. Original fonts/italics are not recovered; ASS placement and size are approximate.'}
    atomic_json(report,audit)
    results = {}
    store = cache/'ocr'; store.mkdir(parents=True,exist_ok=True)
    fingerprint = [VERSION,language,binary.name,platform.mac_ver()[0]]
    keys = {key:hashlib.sha256(json.dumps(fingerprint+[key]).encode()).hexdigest() for key in inputs}
    for key,digest in keys.items():
        if cancel and cancel.exists(): raise Cancelled("Cancelled during OCR.")
        path = store/(digest+'.json')
        if path.exists():
            result = json.loads(path.read_text())
            if result.get('id') == key and not validate_result(result):
                results[key] = result
    pending = [value for key,value in inputs.items() if key not in results]
    for begin in range(0,len(pending),12):
        if cancel and cancel.exists(): raise Cancelled('Cancelled during OCR.')
        progress('ocr',len(results),len(inputs),'Reading PGS images locally; translation follows OCR.')
        batch = pending[begin:begin+12]
        raw = run([str(binary)],stdin=json.dumps(batch),timeout=180,cancel=cancel)
        returned = [json.loads(line) for line in raw.splitlines()]
        expected = {i['id'] for i in batch}
        if len(returned) != len(expected) or {i.get('id') for i in returned} != expected:
            raise RuntimeError('OCR helper returned missing or duplicate image results.')
        for result in returned:
            key = result['id']; results[key] = result
            if not validate_result(result): atomic_json(store/(keys[key]+'.json'),result)
    for key,result in list(results.items()):
        if cancel and cancel.exists(): raise Cancelled("Cancelled during OCR.")
        if suspicious(result) and verifier:
            progress('repairing',0,len(inputs),'Verifying uncertain OCR against the original subtitle image...')
            try:
                fixed,readings=verify_image(result,Path(inputs[key]['path']),verifier)
                result['automatic_checks']=readings
                if fixed:
                    results[key]=fixed;atomic_json(store/(keys[key]+'.json'),fixed)
            except Cancelled:raise
            except (RuntimeError,TimeoutError,ValueError) as exc:result['verification_error']=str(exc)
    flagged = []
    for item in items:
        if cancel and cancel.exists(): raise Cancelled("Cancelled during OCR.")
        result = results[item['image_key']]
        item['lines'] = result.get('lines',[])
        item['issue'] = validate_result(result) or ('Ambiguous single glyph; image readings did not establish the text.' if suspicious(result) else '')
        if result.get('image_verified'):item['image_verification']=result['image_readings']
        if result.get('automatic_checks'):item['automatic_checks']=result['automatic_checks']
        if result.get('verification_error'):item['verification_error']=result['verification_error']
        item['original_bitmap']=bool(item['issue'] and keep_original)
        if item['issue']: flagged.append(item['id'])
    audit['status'] = ('complete_with_originals' if keep_original else 'needs_review') if flagged else 'complete'
    audit['needs_review'] = flagged
    atomic_json(report,audit)
    if flagged and not keep_original:
        raise ReviewRequired(f'OCR needs review at cue(s) {", ".join(map(str,flagged[:12]))}. '
                             f'No translation started. Images and recognized text: {report}')
    width,height = canvas
    header = f'''[Script Info]
Title: PGS OCR source
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
; PGS timing rounded to 10 ms. Positions/sizes approximate; original font unavailable.
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,42,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,20,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    rows = []
    for item in items:
        if cancel and cancel.exists(): raise Cancelled("Cancelled during OCR.")
        # Internal placeholder only. The final renderer replaces this cue with
        # original glyph pixels; it is never sent to the translation model.
        lines = item['lines'] if not item['original_bitmap'] else [{'text':f'PGS_IMAGE_{item["id"]}','height':.5,'y':.2}]
        text = r'\N'.join(line['text'].strip() for line in lines)
        image_h = item['height']+32
        font = max(12,round(sum(line['height']*image_h for line in lines)/len(lines)*1.3))
        x = round(item['x']+item['width']/2)
        y = min(height,round(item['y']+max(line['y']+line['height'] for line in lines)*image_h-16))
        start,end = ass_time(item['start_ticks']),ass_time(item['end_ticks'])
        if start == end: raise PGSError('PGS cue is shorter than ASS timing precision.')
        tags = '{'+f'\\an2\\pos({x},{y})\\fs{font}'+'}'
        rows.append(f'Dialogue: 0,{start},{end},Default,,0,0,0,,{tags}{text}\n')
    data = header+''.join(rows)
    parse(data,'.ass')  # Validate before publishing an OCR source.
    target = root/'source.ass'
    if target.exists():
        if target.read_text() != data:
            raise FileExistsError(f'An OCR source already exists with different contents: {target}')
    else: publish(target,data.encode())
    if cancel and cancel.exists(): raise Cancelled('Cancelled during OCR.')
    progress('ocr',len(inputs),len(inputs),'OCR complete. Checking and translating the extracted text...')
    return target

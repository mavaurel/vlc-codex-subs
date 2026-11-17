"""Fit generated PGS subtitles without rewriting dialogue or timestamps."""
from __future__ import annotations
import hashlib,json,os,re,struct,sys,tempfile,zlib
from pathlib import Path
from .process import run
from .subtitles import parse,SubtitleError


def measures(words,cache,cancel=None):
    if sys.platform!='darwin':
        return {w:sum(100 if ord(c)<0x2e80 else 200 for c in w) for w in words}
    source=Path(__file__).with_name('measure_text.swift')
    key=hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    binary=cache/'bin'/('measure-text-'+key);binary.parent.mkdir(parents=True,exist_ok=True)
    if not binary.exists():
        fd,name=tempfile.mkstemp(dir=binary.parent);os.close(fd)
        try:
            run(['/usr/bin/xcrun','swiftc','-O',str(source),'-o',name],timeout=180,cancel=cancel)
            os.replace(name,binary)
        finally:Path(name).unlink(missing_ok=True)
    result=json.loads(run([str(binary)],stdin=json.dumps(words,ensure_ascii=False),timeout=60,cancel=cancel))
    if len(result)!=len(words) or any(not isinstance(v,(int,float)) or not 0<=v<1e8 for v in result):
        raise ValueError('Invalid native font measurements.')
    return dict(zip(words,result))


def original_drawing(item):
    """Trace visible glyph pixels, never OCR guesses. Input is our grayscale PNG."""
    data=Path(item['image']).read_bytes();offset=8;compressed=b'';w=h=None
    if data[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('Invalid original subtitle image.')
    while offset<len(data):
        size=struct.unpack_from('>I',data,offset)[0];kind=data[offset+4:offset+8];body=data[offset+8:offset+8+size]
        if kind==b'IHDR':
            w,h,depth,color,_,_,interlace=struct.unpack('>IIBBBBB',body)
            if (depth,color,interlace)!=(8,0,0) or w*h>17_000_000:raise ValueError('Unexpected source PNG format.')
        if kind==b'IDAT':compressed+=body
        offset+=size+12
    pixels=zlib.decompress(compressed)
    if len(pixels)!=(w+1)*h:raise ValueError('Corrupt source PNG.')
    paths=[];active={}
    def rect(run,top,bottom):
        a,b=run;paths.append(f'm {a} {top} l {b} {top} {b} {bottom} {a} {bottom}')
    # Remove the OCR-only 16 px border. Merge equal horizontal runs vertically.
    for y in range(16,h-16):
        line=pixels[y*(w+1):(y+1)*(w+1)]
        if line[0]!=0:raise ValueError('Unexpected PNG row filter.')
        row=line[1+16:1+w-16];runs=[];start=None
        for x,v in enumerate(row+b'\xff'):
            if v<200 and start is None:start=x
            elif v>=200 and start is not None:runs.append((start,x));start=None
        now=set(runs)
        for r,top in active.items():
            if r not in now:rect(r,top,y-16)
        active={r:active.get(r,y-16) for r in runs}
    for r,top in active.items():rect(r,top,h-32)
    if not paths:raise ValueError('Original image has no visible glyphs.')
    return ('{'+f'\\an7\\pos({item["x"]},{item["y"]})\\p1\\bord1\\shad0\\1c&HFFFFFF&\\3c&H000000&'+'}'+ ' '.join(paths))


def fit_pgs(raw,cache,original_images=None,cancel=None,measure_fn=None):
    text=raw.decode('utf-8');original_images=original_images or {}
    if 'Title: PGS OCR source' not in text:return raw,[]
    doc=parse(text,'.ass')
    width=int(re.search(r'(?m)^PlayResX:\s*(\d+)',text)[1]);height=int(re.search(r'(?m)^PlayResY:\s*(\d+)',text)[1])
    if not (0<width<=8192 and 0<height<=8192):raise SubtitleError('Invalid ASS canvas.')
    entries=[];vocabulary={' '}
    for c in doc.cues:
        if c.id in original_images:continue
        m=re.fullmatch(r'\{\\an2\\pos\((\d+),(\d+)\)\\fs(\d+)\}(.*)',c.text,re.S)
        if not m:raise SubtitleError('Unexpected formatting in generated PGS subtitle.')
        body=m[4];parts=body.split(r'\N')
        words=[part.split() for part in parts]
        vocabulary.update(w for part in words for w in part)
        entries.append((c,m,body,words))
    sizes=(measure_fn or measures)(sorted(vocabulary),cache,cancel)
    replacements={};report=[];margin_x=width*.07;margin_y=height*.05;available=width-2*margin_x
    def line_width(words,font):return (sum(sizes[w] for w in words)+sizes[' ']*max(0,len(words)-1))*font/100*1.08
    for c,m,body,parts in entries:
        font=min(int(m[3]),round(height*.044));wrapped=[]
        while True:
            wrapped=[]
            for words in parts:
                line=[]
                for word in words:
                    if line and line_width(line+[word],font)>available:wrapped.append(line);line=[]
                    line.append(word)
                if line:wrapped.append(line)
            largest=max((line_width(x,font) for x in wrapped),default=0)
            block_h=len(wrapped)*font*1.3
            if (len(wrapped)<=3 and largest<=available and block_h<=height*.20) or font<=12:break
            font-=1
        if largest>available:
            # Very long unbreakable tokens: compress horizontally, never truncate.
            scale=max(1,int(100*available/largest));largest*=scale/100
        else:scale=100
        x=max(margin_x+largest/2,min(width-margin_x-largest/2,int(m[1])))
        y=max(margin_y+block_h,min(height-margin_y,int(m[2])))
        newbody=r'\N'.join(' '.join(line) for line in wrapped)
        if re.sub(r'\s','',body.replace(r'\N',''))!=re.sub(r'\s','',newbody.replace(r'\N','')):
            raise SubtitleError('Layout unexpectedly changed subtitle text.')
        tags='{'+f'\\an2\\pos({round(x)},{round(y)})\\fs{font}\\fscx{scale}\\q2'+'}'
        replacements[c.id]=tags+newbody
        report.append({'id':c.id,'font':font,'lines':len(wrapped),'width':round(largest,2),'safe_width':round(available,2),'left':round(x-largest/2,2),'right':round(x+largest/2,2),'bottom':round(y),'top':round(y-block_h,2)})
    for c in doc.cues:
        if c.id in original_images:replacements[c.id]=original_drawing(original_images[c.id])
    chunks=[];offset=0
    for c in doc.cues:chunks += [text[offset:c.begin],replacements[c.id]];offset=c.finish
    chunks.append(text[offset:]);result=''.join(chunks)
    checked=parse(result,'.ass',allow_drawings=bool(original_images))
    if [(c.start,c.end) for c in checked.cues]!=[(c.start,c.end) for c in doc.cues]:raise SubtitleError('Layout altered timing.')
    return result.encode(),report

"""Keep generated subtitles accessible after the player switches videos."""
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, unquote

from .pipeline import atomic_json, publish


def media_key(uri):
    value=5381
    for byte in uri.encode('utf-8'):value=(value*33+byte)%4294967296
    return f'{value:08x}'


def remember_job(root,uri,name):
    if not uri:return
    if not re.fullmatch(r'\d+-\d+',name):raise ValueError('Invalid saved job name.')
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    target=root/f'video-{media_key(uri)}.txt'
    if target.exists():
        old=target.read_text().splitlines()
        if (len(old)>1 and unquote(old[1])==uri and re.fullmatch(r'\d+-\d+',old[0])
                and tuple(map(int,old[0].split('-')))>tuple(map(int,name.split('-')))):
            return
    fd,temp=tempfile.mkstemp(dir=root,prefix='.video-index-')
    try:
        with os.fdopen(fd,'w',encoding='ascii') as stream:
            stream.write(name+'\n'+quote(uri,safe='')+'\n')
        os.replace(temp,target)
    finally:Path(temp).unlink(missing_ok=True)


def backfill_history(root):
    import json
    root=Path(root)
    for job in sorted((root/'jobs').glob('*')):
        request=job/'request.json'
        if not request.exists() or not re.fullmatch(r'\d+-\d+',job.name):continue
        try:req=json.loads(request.read_text())
        except (OSError,ValueError):continue
        if not isinstance(req,dict):continue
        if req.get('action')=='translate' and req.get('media'):
            remember_job(root,req['media'],job.name)


def preserve_result(job,media,output,*,save_beside=True,dvd_title=None):
    """Keep the job copy; add a non-overwriting sidecar when the folder permits it."""
    job,output=Path(job),Path(output)
    result={'subtitle':str(output),'job_copy':str(output),'report':str(job/'review.json'),'warning':''}
    if save_beside and media:
        media=Path(media);data=output.read_bytes()
        if media.is_dir():
            from .dvd import folder
            media=folder(media)
            base=media/(media.name+f'.title-{dvd_title or "main"}')
            names=[Path(str(base)+'.ru'+output.suffix),Path(str(base)+'.codex.ru'+output.suffix),
                   Path(str(base)+'.codex-'+job.name+'.ru'+output.suffix)]
        else:
            names=[media.with_suffix('.ru'+output.suffix),media.with_suffix('.codex.ru'+output.suffix),
                   media.with_name(media.stem+'.codex-'+job.name+'.ru'+output.suffix)]
        try:
            for candidate in names:
                try:publish(candidate,data)
                except FileExistsError:
                    if candidate.read_bytes()!=data:continue
                result['subtitle']=str(candidate)
                break
            else:result['warning']='Existing subtitle files were preserved; the result remains in the saved job.'
        except OSError:
            result['warning']='Could not save beside the video; the result remains in the saved job.'
    atomic_json(job/'result.json',result)
    return result

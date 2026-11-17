from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import subprocess
import time
from urllib.parse import quote, unquote

from .codex import Codex
from .media import discover, extract, local_path
from .pipeline import translate
from .process import Cancelled
from .subtitles import read
from .layout import fit_pgs
from .storage import remember_job, preserve_result


def write_status(job: Path, state, done=0, total=0, message="", output="", media=""):
    # Percent encoding keeps arbitrary filenames/messages out of the line protocol.
    values = ["1", state, str(done), str(total), message, output, media]
    temporary = job / ("status-" + str(os.getpid()) + ".tmp")
    temporary.write_text("\n".join(quote(str(v), safe="") for v in values) + "\n", encoding="ascii")
    os.replace(temporary, job / "status.txt")


def cancel_job(job: Path, wait=.75):
    """Request cooperative cancellation; reconcile abandoned and legacy workers too.

    Inspect exact worker arguments rather than trusting a reusable PID. Never
    signal unrelated processes or report cancellation while a worker is alive.
    """
    (job / 'cancel').touch()
    deadline = time.monotonic() + wait
    while True:
        listing = subprocess.run(['ps', '-axww', '-o', 'command='], capture_output=True,
                                 text=True, check=True, timeout=2).stdout
        active = False
        for line in listing.splitlines():
            # ps leaves spaces in job paths unquoted; anchor the interpreter
            # and match the full known worker argument up to its next option.
            if re.match(r'^\S+\s+-m codex_subs worker ' + re.escape(str(job)) + r' --config ', line.strip()):
                active = True
                break
        status = job / 'status.txt'
        values = [unquote(v) for v in status.read_text().splitlines()] if status.exists() else []
        if len(values) > 1 and values[1] in ('done', 'cancelled', 'error', 'discovered'):
            return
        media = json.loads((job / 'request.json').read_text()).get('media', '')
        if not active:
            write_status(job, 'cancelled', message='Cancelled. Saved OCR and reviewed chunks are retained.', media=media)
            return
        if time.monotonic() >= deadline:
            # Do not overwrite an acknowledgement racing with this check.
            return
        time.sleep(.04)


def resolve_source(spec, media, root, config, cancel, progress=lambda *args:None, verifier=None):
    ocr_root = Path(config.get('ocr_root', root))
    ocr_cache = Path(config.get('ocr_cache',root.parent.parent/'cache'))
    if spec['kind'] == 'dvd_track' or (spec['kind'] == 'track' and media and media.is_dir()):
        from .dvd import extract as extract_dvd, scan, select_title
        if media is None or not media.is_dir():
            raise ValueError('DVD tracks require a local DVD folder.')
        value = str(spec['value'])
        if spec['kind'] == 'track':
            title = select_title(scan(media,config,cancel),config.get('dvd_title'))['title']
            value = f'{title}:{int(value)}'
        return extract_dvd(media,value,ocr_root,{**config,'ocr_cache':ocr_cache},cancel,progress,verifier)
    if spec["kind"] == "file":
        path = local_path(spec["value"])
        if path.suffix.lower() == '.sup':
            from .ocr import convert
            folder = ocr_root / ('pgs-file-'+hashlib.sha256(str(path).encode()).hexdigest()[:16])
            return convert(path,folder,cache=ocr_cache,language=config.get('ocr_language',''),cancel=cancel,progress=progress,
                           verifier=verifier,keep_original=config.get('keep_original',True))
        return path
    if spec["kind"] == "track":
        if media is None:
            raise ValueError("Embedded tracks require a media file.")
        return extract(media, int(spec["value"]), ocr_root,
                       config.get("ffmpeg", "ffmpeg"), config.get("ffprobe", "ffprobe"), cancel,
                       cache=ocr_cache,progress=progress,ocr_language=config.get('ocr_language',''),
                       verifier=verifier,keep_original=config.get('keep_original',True))
    raise ValueError("Unknown source kind.")


def source_images(path,generated_root,*,all_images=False):
    if not path.resolve().is_relative_to(Path(generated_root).resolve()):return {}
    metadata=path.parent/'ocr.json'
    if path.name!='source.ass' or not metadata.exists():return {}
    values={c['id']:c for c in json.loads(metadata.read_text())['cues'] if all_images or c.get('original_bitmap')}
    for value in values.values():
        if not Path(value['image']).resolve().is_relative_to((path.parent/'images').resolve()):
            raise ValueError('Original subtitle image is outside its generated source directory.')
    return values


def reference_document(path,generated_root,encoding='utf-8-sig'):
    doc=read(path,encoding);images=source_images(path,generated_root)
    doc.cues=[c for c in doc.cues if c.id not in images]
    return doc


def worker(job: Path, config: dict):
    if config.get("executable_path"):
        os.environ["PATH"] = config["executable_path"]
    req = json.loads((job / "request.json").read_text(encoding="utf-8"))
    cancel, media_uri = job / "cancel", req.get("media", "")
    try:
        if cancel.exists():
            raise Cancelled("Cancelled.")
        write_status(job, "starting", message="Starting…", media=media_uri)
        media = local_path(media_uri) if media_uri else None
        if media and media.is_dir():
            from .dvd import requested_title
            config = {**config, 'dvd_title':requested_title(media_uri)}
        if req["action"] == "discover":
            if not media:
                raise ValueError("Open a local video in VLC first.")
            if media.is_dir():
                choices, warnings = discover(media, cancel=cancel, dvd_config=config, dvd_title=config['dvd_title'])
            else:
                choices, warnings = discover(media, config.get("ffprobe", "ffprobe"), cancel)
            lines = ["\t".join(quote(str(c[k]), safe="") for k in ("kind", "value", "label")) for c in choices]
            (job / "choices.tsv").write_text("\n".join(lines), encoding="ascii")
            (job / "discovery-version.txt").write_text("1.3\n",encoding="ascii")
            write_status(job, "discovered", message=f"Found {len(choices)} subtitle sources. " + " ".join(warnings), media=media_uri)
            return
        if req["action"] != "translate":
            raise ValueError("Unknown job action.")
        remember_job(job.parent.parent,media_uri,job.name)
        mode=req.get('profile') or config.get('profile','light')
        chunk_size=config.get('chunk_size',64) if mode=='light' else 32
        workers=config.get('workers',4) if mode=='light' else 1
        service_tier=config.get('service_tier','') if mode=='light' else ''
        engine = Codex(config.get('codex','codex'),req.get('model','') or config.get('model',''),
                       config.get('timeout',600),cancel,reasoning='low' if mode=='light' else 'medium',
                       service_tier=service_tier)
        def preparing(state, done, total, message):
            write_status(job,state,done,total,message,media=media_uri)
        primary_path = resolve_source(req["primary"], media, job, config, cancel,preparing,engine)
        primary = read(primary_path, config.get("encoding", "utf-8-sig"))
        references, seen = [], {str(primary_path.resolve())}
        for spec in req.get("references", []):
            path = resolve_source(spec, media, job, config, cancel,preparing,engine)
            if str(path.resolve()) not in seen:
                seen.add(str(path.resolve()))
                references.append((spec.get("label", path.name), reference_document(path,job,config.get("encoding", "utf-8-sig"))))
        output = job / ("Russian" + primary.suffix)
        original_images=source_images(primary_path,job)
        resume=None
        resume_path=job/'resume-reviewed.json'
        if resume_path.exists():
            saved=json.loads(resume_path.read_text())
            if saved['source_sha256']!=hashlib.sha256(primary.text.encode()).hexdigest():
                raise ValueError('Saved approvals belong to a different subtitle source.')
            resume=saved['cues']
        def finish(raw,images,audit):
            rendered,layout=fit_pgs(raw,job.parent.parent/'cache',images,cancel)
            audit['layout']=layout
            if resume:
                audit['resumed_approved_cues']=len(resume)
                audit['resumed_provenance']=saved.get('provenance',{})
            return rendered

        done_message=''
        def progress(state, done, total, message):
            nonlocal done_message
            if state=='done':done_message=message
            write_status(job, state, done, total, message, str(output) if state == "done" else "", media_uri)

        translate(primary, references, engine, output, cache=job.parent.parent / "cache",
                  report=job / "review.json", chunk_size=chunk_size,
                  glossary=config.get("glossary", {}), progress=progress, cancel=cancel,
                  keep_original=config.get('keep_original',True),original_images=original_images,
                  finish_output=finish,resume_rows=resume,
                  image_evidence=source_images(primary_path,job,all_images=True),workers=workers)
        dvd_title = int(req['primary']['value'].split(':')[0]) if req['primary']['kind']=='dvd_track' else config.get('dvd_title')
        result=preserve_result(job,media,output,save_beside=config.get('save_beside_video',True),dvd_title=dvd_title)
        if result['warning']:done_message+=' '+result['warning']
        write_status(job,'done',len(primary.cues),len(primary.cues),done_message,result['subtitle'],media_uri)
    except Exception as exc:
        write_status(job, "cancelled" if isinstance(exc, Cancelled) else "error", message=str(exc), media=media_uri)


def main():
    parser = argparse.ArgumentParser(description="Translate subtitles in any language into Russian using Codex CLI.")
    sub = parser.add_subparsers(dest="command", required=True)
    worker_parser = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("job", type=Path)
    worker_parser.add_argument("--config", type=Path, required=True)
    launcher = sub.add_parser("start-job", help=argparse.SUPPRESS)
    launcher.add_argument("job", type=Path)
    launcher.add_argument("--config", type=Path, required=True)
    launcher.add_argument("--wait", type=float, default=0)
    stopper = sub.add_parser("cancel-job", help=argparse.SUPPRESS)
    stopper.add_argument("job", type=Path)
    scan = sub.add_parser("scan", help="List embedded subtitle tracks and matching sidecar files")
    scan.add_argument("media")
    scan.add_argument("--ffprobe", default="ffprobe")
    tr = sub.add_parser("translate", help="Translate and review a subtitle file or embedded track")
    source = tr.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", help="Primary SRT/ASS/SSA/WebVTT or PGS .sup file; any language")
    source.add_argument("--track", type=int, help="Primary absolute stream index from scan")
    tr.add_argument("--media", help="Local video used for --track / --reference-track")
    tr.add_argument("--reference", action="append", default=[], help="Supporting subtitle file; repeat for multiple languages")
    tr.add_argument("--reference-track", action="append", type=int, default=[], help="Supporting absolute embedded stream index; repeatable")
    tr.add_argument("--output", type=Path, required=True)
    tr.add_argument("--model", default="gpt-6.1-sol", help="Codex model name; default gpt-6.1-sol; empty reads your Codex model preference")
    tr.add_argument("--codex", default="codex")
    tr.add_argument("--ffmpeg", default="ffmpeg")
    tr.add_argument("--ffprobe", default="ffprobe")
    tr.add_argument("--encoding", default="utf-8-sig")
    tr.add_argument("--ocr-language", default="", help="OCR language for unlabelled PGS/.sup, e.g. en or fr; default auto")
    tr.add_argument("--glossary", type=Path, help="JSON object: source term to Russian term")
    tr.add_argument("--chunk-size", type=int, default=None, help="0: automatic large requests; split on incomplete output")
    tr.add_argument('--mode',choices=['light','careful'],default='light')
    tr.add_argument('--workers',type=int,default=None,help='Parallel batches, 1–15; Light defaults to 4, Careful to 1')
    tr.add_argument('--fast',action='store_true',help='Use Codex Fast service (higher account usage)')
    tr.add_argument('--stop-on-uncertain',action='store_true',help='Stop instead of retaining unresolved source captions')
    tr.add_argument("--timeout", type=int, default=600, help="Timeout per Codex call, seconds")
    tr.add_argument("--cache", type=Path, help="Checkpoint directory for resumable jobs")
    args = parser.parse_args()
    try:
        if args.command == "start-job":
            job = args.job.resolve()
            with (job / "worker.log").open("ab") as log:
                subprocess.Popen([sys.executable, "-m", "codex_subs", "worker", str(job),
                                  "--config", str(args.config.resolve())],
                                 cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True)
            # Only discovery may wait briefly. Translation launches return immediately.
            deadline = time.monotonic() + max(0, min(args.wait, 1.2))
            while time.monotonic() < deadline:
                path = job / "status.txt"
                lines = path.read_text().splitlines() if path.exists() else []
                if len(lines) > 1 and lines[1] in ("discovered", "error", "done", "cancelled"):
                    break
                time.sleep(0.04)
        elif args.command == "cancel-job":
            cancel_job(args.job.resolve())
        elif args.command == "worker":
            worker(args.job.resolve(), json.loads(args.config.read_text(encoding="utf-8")))
        elif args.command == "scan":
            from .dvd import requested_title
            choices, warnings = discover(local_path(args.media), args.ffprobe, dvd_title=requested_title(args.media))
            print(json.dumps({"sources": choices, "warnings": warnings}, ensure_ascii=False, indent=2))
        else:
            with tempfile.TemporaryDirectory(prefix="vlc-subs-extract-") as directory:
                root = Path(directory)
                media = local_path(args.media) if args.media else None
                config = vars(args)
                from .dvd import requested_title
                config['dvd_title'] = requested_title(args.media) if args.media else None
                config['keep_original']=not args.stop_on_uncertain
                output = args.output.expanduser().resolve()
                config['ocr_root'] = output.with_name(output.name+'.sources')
                config['ocr_cache'] = args.cache or output.parent/'.codex-subs-cache'
                preparing = lambda state,done,total,msg: print(f'{state}: {done}/{total} {msg}',flush=True)
                engine=Codex(args.codex,args.model,args.timeout,reasoning='low' if args.mode=='light' else 'medium',
                             service_tier='fast' if args.fast else '')
                primary_spec = {"kind": "file", "value": args.source} if args.source else {"kind": "track", "value": args.track}
                source_path = resolve_source(primary_spec, media, root, config, None,preparing,engine)
                refs = [(str(p), reference_document(resolve_source({'kind':'file','value':p},media,root,config,None,preparing,engine),config['ocr_root'],args.encoding)) for p in args.reference]
                refs += [(f"embedded stream {i}", reference_document(resolve_source({"kind": "track", "value": i}, media, root, config, None,preparing,engine),config['ocr_root']))
                         for i in args.reference_track]
                output = args.output.expanduser().resolve()
                report = output.with_name(output.name + ".review.json")
                if report.exists():
                    # Keep prior reports; a retry writes to a unique companion report.
                    fd, path = tempfile.mkstemp(prefix=output.name + ".review-", suffix=".json", dir=output.parent)
                    os.close(fd)
                    report = Path(path)
                glossary = json.loads(args.glossary.read_text(encoding="utf-8")) if args.glossary else {}
                originals=source_images(source_path,config['ocr_root'])
                def finish(raw,images,audit):
                    result,layout=fit_pgs(raw,Path(config['ocr_cache']),images)
                    audit['layout']=layout
                    return result
                translate(read(source_path, args.encoding), refs, engine,
                          output, cache=args.cache or output.parent / ".codex-subs-cache", report=report,
                          chunk_size=args.chunk_size if args.chunk_size is not None else (64 if args.mode=='light' else 32), glossary=glossary,
                          keep_original=not args.stop_on_uncertain,original_images=originals,finish_output=finish,
                          image_evidence=source_images(source_path,config['ocr_root'],all_images=True),
                          workers=args.workers if args.workers is not None else (4 if args.mode=='light' else 1),
                          progress=lambda state, done, total, msg: print(f"{state}: {done}/{total} {msg}", flush=True))
                print(f"Review report: {report}")
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Error: {exc or 'Interrupted'}", file=sys.stderr)
        return 1
    return 0

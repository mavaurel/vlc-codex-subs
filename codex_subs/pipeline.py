from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event, RLock

from .process import Cancelled
from .subtitles import Document, SubtitleError, align, parse


PROMPT_VERSION = "multisource-3-recovery"


class ReviewRequired(RuntimeError):
    pass


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def publish(path: Path, data: bytes):
    """Atomically publish a complete file without clobbering an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".subtitle-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def validated(response: dict, cues) -> list[dict]:
    if not isinstance(response, dict) or set(response) != {"cues"} or not isinstance(response["cues"], list):
        raise SubtitleError("Invalid model response shape.")
    rows = response["cues"]
    expected = {c.id: c for c in cues}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "text", "uncertain", "note"}:
            raise SubtitleError("Invalid cue response shape.")
        i = row["id"]
        if type(i) is not int or i not in expected or i in seen:
            raise SubtitleError("Model returned duplicate or unexpected cue IDs.")
        if type(row["uncertain"]) is not bool or not isinstance(row["note"], str) or not isinstance(row["text"], str):
            raise SubtitleError("Invalid cue response types.")
        expected[i].restore(row["text"])
        seen.add(i)
    if seen != set(expected):
        raise SubtitleError("Model omitted one or more cues.")
    return sorted(rows, key=lambda r: r["id"])


def translate(primary: Document, references: list[tuple[str, Document]], engine,
              output: Path, *, cache: Path, report: Path, chunk_size=0,
              glossary: dict | None = None, progress=lambda *args: None,
              cancel: Path | None = None, keep_original=False,
              original_images=None, finish_output=None, resume_rows=None, image_evidence=None,
              workers=1) -> Path:
    if not 0 <= chunk_size <= 10000:
        raise ValueError('Chunk size must be 0 (automatic) or 1–10000.')
    if type(workers) is not int or not 1 <= workers <= 15:
        raise ValueError('Workers must be 1–15.')
    lock, abort = RLock(), Event()
    class StopSignal:
        def exists(self): return abort.is_set() or bool(cancel and cancel.exists())
    signal = StopSignal()
    def check_cancel():
        if signal.exists(): raise Cancelled('Cancelled.')
    if output.exists():
        raise FileExistsError(f'Output already exists: {output}. Choose a different output path.')
    if output.suffix.lower() != primary.suffix:
        raise ValueError(f'Output extension must be {primary.suffix} to preserve the source format.')
    original_images = dict(original_images or {})
    image_evidence = image_evidence or {}
    if (original_images or image_evidence) and not finish_output:
        raise ValueError('Original subtitle images require a final image renderer.')
    glossary = glossary or {}
    if not isinstance(glossary,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in glossary.items()):
        raise ValueError('Glossary must be a JSON object mapping source terms to Russian terms.')
    alignments,coverage = [],[]
    for label,doc in references:
        matched=align(primary,doc)
        ratio=sum(bool(h) for h in matched.values())/len(primary.cues)
        coverage.append({'source':label,'primary_cues_with_overlap':round(ratio,3)})
        if ratio < .6:
            raise ValueError(f'Reference {label!r} overlaps only {ratio:.0%} of primary cues. Use the same release/cut, synchronize its timestamps, or remove this reference.')
        alignments.append((label,matched))
    # Batch size is scheduling, not content identity; smaller retries reuse approvals.
    key_data=[PROMPT_VERSION,primary.suffix,primary.text,[(l,d.text) for l,d in references],
              getattr(engine,'model',''),getattr(engine,'reasoning',''),glossary,keep_original,
              sorted(original_images), sorted((i,c['image_key']) for i,c in image_evidence.items())]
    digest=hashlib.sha256(json.dumps(key_data,ensure_ascii=False).encode()).hexdigest()
    checkpoint=cache/f'{digest}.json';completed=[];fallbacks=[];pending=[]
    draft_path=cache/f'{digest}.draft.json'
    drafts={}
    if draft_path.exists():
        saved_drafts=json.loads(draft_path.read_text())
        if saved_drafts.get('key')!=digest:raise ValueError('Draft cache identity mismatch.')
        draft_rows=saved_drafts.get('cues',[])
        draft_ids={r['id'] for r in draft_rows}
        validated({'cues':draft_rows},[c for c in primary.cues if c.id in draft_ids])
        drafts={r['id']:r for r in draft_rows}
    if checkpoint.exists():
        saved=json.loads(checkpoint.read_text());assert saved['key']==digest
        completed=saved.get('cues',[]);fallbacks=saved.get('originals_kept',[]);pending=saved.get('pending',[])
    elif resume_rows:
        completed=list(resume_rows)
    validated({'cues':completed},primary.cues[:len(completed)])
    pending_ids={r['id'] for r in pending}
    validated({'cues':pending},[c for c in primary.cues if c.id in pending_ids])
    if any(i<=len(completed) for i in pending_ids):
        raise ValueError('Checkpoint contains overlapping approvals.')
    if any(r['uncertain'] for r in completed+pending):
        raise ValueError('Unreviewed checkpoint; refusing to reuse it.')
    ready={r['id']:r for r in completed+pending}
    for fallback in fallbacks:
        if fallback['kind']=='bitmap' and fallback['id'] in image_evidence:
            original_images[fallback['id']]=image_evidence[fallback['id']]
    audit={'version':PROMPT_VERSION,'primary_cues':len(primary.cues),'references':coverage,
           'model':getattr(engine,'model',''),'reasoning':getattr(engine,'reasoning',''),
           'status':'running','cues':completed.copy(),'originals_kept':fallbacks,
           'approved_out_of_order':pending,'reviewed_cues_count':len(ready),'workers':workers,
           'service_tier':getattr(engine,'service_tier','') or 'standard',
           'batch_mode':'automatic' if not chunk_size else chunk_size,
           'limitation':'Model review is not a zero-error guarantee. Unresolved cues retain their original source.'}
    atomic_json(report,audit)

    def emit(state,done,total,message):
        with lock: progress(state,len(ready),total,message)

    def throttled(seconds,limit):
        with lock:
            audit.setdefault('rate_limits',[]).append({'at':time.time(),'retry_after_seconds':round(seconds,1),'workers':limit})
            audit['effective_workers']=limit
            atomic_json(report,audit)
        emit('waiting',0,len(primary.cues),f'Codex rate limit: waiting {math.ceil(seconds)}s before retry; concurrency reduced to {limit}.')
    if hasattr(engine,'configure_parallel'):
        engine.configure_parallel(workers,throttled,
                                  lambda limit:emit('running',0,len(primary.cues),f'Retrying Codex after rate-limit wait ({limit} workers).'))

    def payload(cue):
        text,tokens=cue.protected()
        return {'id':cue.id,'start':cue.start,'end':cue.end,'speaker':cue.speaker,'text':text,
                'formatting':{f'⟦KEEP_{i}⟧':t for i,t in enumerate(tokens)},
                'references':[{'source':label,'lines':[{'start':r.start,'end':r.end,'text':r.text,'speaker':r.speaker}
                               for r in matched[cue.id]]} for label,matched in alignments]}

    def context_for(cues,radius=5):
        ids={c.id for c in cues}
        lo=max(0,min(ids)-1-radius);hi=min(len(primary.cues),max(ids)+radius)
        return [payload(c) for c in primary.cues[lo:hi] if c.id not in ids and c.id not in original_images]

    def preserve(cue,reason,kind='text'):
        with lock:
            if not any(f['id']==cue.id for f in fallbacks):
                fallbacks.append({'id':cue.id,'reason':reason,'kind':kind,'translated':False})
        return {'id':cue.id,'text':cue.protected()[0],'uncertain':False,'note':'Original kept untranslated: '+reason}

    def process(cues,active_engine):
        usable=[c for c in cues if c.id not in original_images]
        by_id={c.id:preserve(c,'OCR could not be established from the image.','bitmap') for c in cues if c.id in original_images}
        if not usable:return [by_id[c.id] for c in cues]
        with lock:
            original_ids={f['id'] for f in fallbacks}
            previous=[ready[i] for i in sorted(ready) if i<cues[0].id and i not in original_ids][-12:]
        data={'targets':[payload(c) for c in usable],'context_only':context_for(usable),
              'previous_approved':previous,'terminology':glossary}
        with lock: needed=[c for c in usable if c.id not in drafts]
        if needed:
            tier='Fast' if getattr(active_engine,'service_tier','')=='fast' else 'standard'
            emit('translating',0,len(primary.cues),f'Translating {len(needed)} captions with {getattr(active_engine,"model","")} ({getattr(active_engine,"reasoning","default")} reasoning, {tier}, {workers} workers)...')
            draft_data={**data,'targets':[payload(c) for c in needed], 'context_only':context_for(needed)}
            for attempt in range(2):
                try:
                    check_cancel()
                    new_draft=validated(active_engine.complete(draft_data),needed);break
                except SubtitleError as exc:
                    if attempt:raise
                    draft_data['repair_required']=str(exc)
            with lock:
                drafts.update({r['id']:r for r in new_draft})
                atomic_json(draft_path,{'key':digest,'cues':list(drafts.values())})
        with lock: draft=[drafts[c.id] for c in usable]
        tier='Fast' if getattr(active_engine,'service_tier','')=='fast' else 'standard'
        emit('reviewing',0,len(primary.cues),f'Checking fidelity of {len(usable)} captions ({tier}, {workers} workers)...')
        review_data={**data,'draft':draft}
        for attempt in range(2):
            try:
                check_cancel()
                rows=validated(active_engine.complete(review_data,review=True),usable);break
            except SubtitleError as exc:
                if attempt:raise
                review_data['repair_required']=str(exc)
        if getattr(active_engine,'last_review',None):
            with lock: audit.setdefault('reviews',[]).append(active_engine.last_review)
        flagged=[r for r in rows if r['uncertain']]
        if flagged:
            # Retry only the flagged cues, with wider dialogue context. Then verify
            # the repair in a separate call; never clear the model's flags in code.
            emit('repairing',0,len(primary.cues),f'Rechecking {len(flagged)} ambiguous captions with more context...')
            for begin in range(0,len(flagged),8):
                group=flagged[begin:begin+8]
                ids={r['id'] for r in group};targets=[c for c in usable if c.id in ids]
                repair={'targets':[payload(c) for c in targets],'context_only':context_for(targets,12),
                        'draft':group,'flagged':group,'previous_approved':previous,'terminology':glossary}
                pictures=[image_evidence[c.id] for c in targets if c.id in image_evidence]
                extra={}
                if pictures:
                    repair['image_evidence']=[{'cue_id':p['id'],'attachment':i+1} for i,p in enumerate(pictures)]
                    extra={'images':[Path(p['image']) for p in pictures]}
                try:
                    check_cancel()
                    proposed=validated(active_engine.complete(repair,review=True,conservative=True,**extra),targets)
                    check_cancel()
                    confirmed=validated(active_engine.complete({**repair,'draft':proposed,'verification':'Verify independently against the source, not just the proposed wording.'},review=True,conservative=True,**extra),targets)
                    fixes={r['id']:r for r in confirmed}
                    rows=[fixes.get(r['id'],r) for r in rows]
                    with lock: audit.setdefault('ambiguity_reviews',[]).append({'initial_flags':group,'proposed':proposed,'result':confirmed,'image_cues':[p['id'] for p in pictures]})
                except (SubtitleError,TimeoutError) as exc:
                    with lock: audit.setdefault('repair_errors',[]).append(str(exc))
            flagged=[r for r in rows if r['uncertain']]
        if flagged and not keep_original:
            with lock: audit['needs_review']=flagged
            raise ReviewRequired('Unresolved source ambiguity at cue(s) '+', '.join(str(r['id']) for r in flagged)+f'. No final subtitle created. See {report}')
        for row in rows:
            cue=primary.cues[row['id']-1]
            if row['uncertain'] and cue.id in image_evidence:
                with lock: original_images[cue.id]=image_evidence[cue.id]
                by_id[cue.id]=preserve(cue,row['note'],'bitmap')
            else:
                by_id[row['id']]=preserve(cue,row['note']) if row['uncertain'] else row
        return [by_id[c.id] for c in cues]

    def commit(rows):
        with lock:
            for row in rows:
                if row['id'] in ready:raise ValueError('Duplicate batch approval.')
                ready[row['id']]=row
            completed.clear()
            while len(completed)+1 in ready:completed.append(ready[len(completed)+1])
            pending=[ready[i] for i in sorted(ready) if i>len(completed)]
            kept=[f for f in fallbacks if f['id'] in ready]
            atomic_json(checkpoint,{'key':digest,'cues':completed,'pending':pending,'originals_kept':kept})
            audit.update(cues=completed.copy(),approved_out_of_order=pending,reviewed_cues_count=len(ready))
            atomic_json(report,audit)
            emit('running',0,len(primary.cues),f'{len(ready)} of {len(primary.cues)} captions reviewed; {workers} workers.')

    def run_batch(cues,active_engine):
        check_cancel()
        try: rows=process(cues,active_engine)
        except (SubtitleError,TimeoutError) as exc:
            if len(cues)>1:
                with lock:
                    audit.setdefault('split_retries',[]).append({'start_id':cues[0].id,'size':len(cues),'reason':str(exc)})
                    atomic_json(report,audit)
                mid=len(cues)//2
                run_batch(cues[:mid],active_engine);run_batch(cues[mid:],active_engine)
                return
            if not keep_original:raise
            cue=cues[0]
            if cue.id in image_evidence:
                with lock: original_images[cue.id]=image_evidence[cue.id]
            rows=[preserve(cue,'No valid reviewed response after automatic retries: '+str(exc),
                           'bitmap' if cue.id in original_images else 'text')]
        commit(rows)

    def batches():
        remaining=[c for c in primary.cues if c.id not in ready]
        start=0
        while start<len(remaining):
            size=min(chunk_size or len(remaining),len(remaining)-start)
            if not chunk_size:
                # Conservative output budget for automatic/whole-file requests.
                while size>1 and sum(max(64,len(c.text)*1.25+40) for c in remaining[start:start+size])>80000:
                    size=(size+1)//2
            for offset in range(1,size):
                if remaining[start+offset].id!=remaining[start+offset-1].id+1:
                    size=offset;break
            yield remaining[start:start+size]
            start+=size

    try:
        work=list(batches())
        def active_engine():
            return engine.fork(cancel=signal) if hasattr(engine,'fork') else engine
        if workers==1:
            for cues in work:run_batch(cues,active_engine())
        else:
            pool=ThreadPoolExecutor(max_workers=workers)
            futures=[]
            try:
                futures=[pool.submit(run_batch,cues,active_engine()) for cues in work]
                for future in as_completed(futures):future.result()
            except BaseException:
                abort.set()
                for future in futures:future.cancel()
                raise
            finally:pool.shutdown(wait=True,cancel_futures=True)
        check_cancel()
        if len(completed)!=len(primary.cues):raise SubtitleError('Missing reviewed captions after batch completion.')
        rendered=primary.render({r['id']:r['text'] for r in completed})
        if finish_output:rendered=finish_output(rendered,original_images,audit)
        if original_images and any(f'PGS_IMAGE_{i}' in rendered.decode('utf-8') for i in original_images):
            raise SubtitleError('Original subtitle image was not rendered; refusing to publish a placeholder.')
        parsed=parse(rendered.decode('utf-8'),primary.suffix,allow_drawings=bool(original_images))
        if [(c.id,c.start,c.end) for c in parsed.cues]!=[(c.id,c.start,c.end) for c in primary.cues]:
            raise SubtitleError('Final subtitle failed the timing/cue-count check.')
        publish(output,rendered)
        audit['status']='complete_with_warnings' if fallbacks else 'complete';atomic_json(report,audit)
        message=f'Complete. {len(fallbacks)} original captions kept untranslated; see {report}' if fallbacks else str(output)
        progress('done',len(completed),len(primary.cues),message)
        return output
    except BaseException as exc:
        audit['status']='cancelled' if isinstance(exc,Cancelled) else 'needs_review' if isinstance(exc,ReviewRequired) else 'error'
        audit['error']=str(exc);atomic_json(report,audit);raise

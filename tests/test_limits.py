import json
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from codex_subs.codex import Codex, check_throttling
from codex_subs.limits import RequestGate
from codex_subs.process import Cancelled, RateLimited, run
from codex_subs.pipeline import translate
from codex_subs.subtitles import parse


LOG = ('2026-10-26T07:30:37.754045Z  WARN codex_core::responses_retry: '
       'stream disconnected - retrying sampling request (1/5 in 120.99830725s)... '
       'sampling_error=exceeded retry limit, last status: 429 Too Many Requests\n')


class LimitTests(unittest.TestCase):
    def test_real_cli_diagnostic_exposes_server_retry_delay(self):
        with self.assertRaises(RateLimited) as raised: check_throttling(LOG)
        self.assertAlmostEqual(raised.exception.retry_after,120.99830725)
        check_throttling('DATA:\n{"text":"429 Too Many Requests"}')

    def test_streaming_rate_limit_interrupts_cli_internal_two_minute_wait(self):
        script='import sys,time; sys.stderr.write('+repr(LOG)+'); sys.stderr.flush(); time.sleep(30)'
        start=time.monotonic()
        with self.assertRaises(RateLimited):
            run([sys.executable,'-c',script],timeout=5,stderr_callback=check_throttling)
        self.assertLess(time.monotonic()-start,2)

    def test_penalty_is_shared_reduces_concurrency_and_can_be_cancelled(self):
        events=[]
        gate=RequestGate(4,lambda seconds,limit:events.append(limit))
        gate.penalize(30);gate.penalize(30)
        self.assertEqual(events,[2,1])
        class Cancel:
            def exists(self):return True
        start=time.monotonic()
        with self.assertRaises(Cancelled):
            with gate.slot(Cancel()):self.fail('Started during rate-limit wait')
        self.assertLess(time.monotonic()-start,.1)

    def test_cooldown_resumes_once_then_serializes_reduced_workers(self):
        resumes=[];gate=RequestGate(2,on_resume=lambda limit:resumes.append(limit))
        gate.penalize(.03)
        entered=Event();release=Event();order=[]
        def first():
            with gate.slot():order.append(1);entered.set();release.wait(2)
        def second():
            with gate.slot():order.append(2)
        a=Thread(target=first);b=Thread(target=second)
        a.start();self.assertTrue(entered.wait(1));b.start()
        time.sleep(.03);self.assertEqual(order,[1]);release.set()
        a.join(2);b.join(2)
        self.assertEqual(order,[1,2]);self.assertEqual(resumes,[1]);self.assertEqual(gate.active,0)

    def test_codex_forks_share_one_gate_and_retry_rate_limit_through_it(self):
        events=[];engine=Codex(model='gpt-6.1-sol',service_tier='fast')
        engine.configure_parallel(4,lambda seconds,limit:events.append(limit))
        child=engine.fork();self.assertIs(child.gate,engine.gate)
        calls=[]
        def fake(argv,**kwargs):
            calls.append(argv)
            if len(calls)==1:raise RateLimited(0)
            Path(argv[argv.index('--output-last-message')+1]).write_text('{"ok":true}')
            return ''
        with patch('codex_subs.codex.run',side_effect=fake):
            self.assertEqual(child.request('test',{'type':'object'}),{'ok':True})
        self.assertEqual(events,[2]);self.assertEqual(len(calls),2)
        self.assertEqual(engine.gate.limit,2)

    def test_parallel_pipeline_reports_throttling_and_completes_after_shared_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=parse('1\n00:00:01,000 --> 00:00:02,000\nNo.\n\n2\n00:00:03,000 --> 00:00:04,000\nNo.\n','.srt')
            engine=Codex(model='gpt-6.1-sol',service_tier='fast')
            calls=[];progress=[]
            def fake(argv,**kwargs):
                calls.append(argv)
                if len(calls)==1:raise RateLimited(0)
                data=json.loads(kwargs['stdin'].split('\nDATA:\n',1)[1])
                ids=[c['id'] for c in data['targets']]
                spec=json.loads(Path(argv[argv.index('--output-schema')+1]).read_text())
                if 'reviewed_ids' in spec['required']:
                    response={'reviewed_ids':ids,'changes':[]}
                else:
                    response={'cues':[{'id':i,'text':'Нет.','uncertain':False,'note':''} for i in ids]}
                Path(argv[argv.index('--output-last-message')+1]).write_text(json.dumps(response))
                return ''
            with patch('codex_subs.codex.run',side_effect=fake):
                translate(source,[],engine,root/'Russian.srt',cache=root/'cache',report=root/'review.json',
                          chunk_size=1,workers=2,progress=lambda *args:progress.append(args))
            report=json.loads((root/'review.json').read_text())
            self.assertEqual(report['status'],'complete')
            self.assertEqual(report['workers'],2)
            self.assertEqual(report['effective_workers'],1)
            self.assertEqual(len(report['rate_limits']),1)
            self.assertTrue(any(p[0]=='waiting' and 'rate limit' in p[3] for p in progress))


if __name__=='__main__':unittest.main()

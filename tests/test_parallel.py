import json
from pathlib import Path
import tempfile
from threading import Barrier, Event, Lock, Thread
import time
import unittest
from unittest.mock import patch

from codex_subs.codex import Codex
from codex_subs.pipeline import translate
from codex_subs.process import Cancelled
from codex_subs.subtitles import parse, read
from test_pipeline import FakeEngine


class ParallelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.doc = parse(''.join(f'{i}\n00:00:01,000 --> 00:00:02,000\nNo.\n\n' for i in range(1, 9)), '.srt')
        self.output = self.root/'Russian.srt'
        self.report = self.root/'review.json'

    def run_translation(self, engine, **kwargs):
        return translate(self.doc, [], engine, self.output, cache=self.root/'cache',
                         report=self.report, keep_original=True, **kwargs)

    def test_parallel_reviews_survive_out_of_order_completion_and_interruption(self):
        release = Event()
        class Engine(FakeEngine):
            def complete(self, data, **kwargs):
                if data['targets'][0]['id'] == 1:
                    if not release.wait(4): raise RuntimeError('Test did not release first batch')
                    raise Cancelled('Interrupted before first batch completed')
                return super().complete(data, **kwargs)
        errors = []
        progress = []
        def work():
            try: self.run_translation(Engine(), workers=4, chunk_size=2, progress=lambda *args: progress.append(args))
            except BaseException as exc: errors.append(exc)
        thread = Thread(target=work)
        thread.start()
        try:
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                if self.report.exists():
                    report = json.loads(self.report.read_text())
                    if report.get('reviewed_cues_count') == 6: break
                time.sleep(.01)
            else: self.fail('Parallel batches did not complete while first batch was blocked')
            self.assertEqual(report['cues'], [])
            self.assertEqual([r['id'] for r in report['approved_out_of_order']], [3,4,5,6,7,8])
            self.assertFalse(self.output.exists())
            self.assertTrue(any(p[1]==6 for p in progress))
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], Cancelled)
        engine = FakeEngine()
        self.run_translation(engine, workers=2, chunk_size=1)
        self.assertEqual(sorted({c['id'] for d,_ in engine.calls for c in d['targets']}), [1,2])
        self.assertEqual([c.id for c in read(self.output).cues], list(range(1,9)))
        final = json.loads(self.report.read_text())
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['approved_out_of_order'], [])

    def test_parallel_cancellation_stops_sibling_workers(self):
        entered = Event()
        mutex = Lock()
        state = {'active': 0, 'peak': 0}
        class Engine(FakeEngine):
            def __init__(self, cancel=None): super().__init__();self.cancel=cancel
            def fork(self, cancel=None): return Engine(cancel)
            def complete(self, data, **kwargs):
                with mutex:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                    if state['active']>=4: entered.set()
                try:
                    if not entered.wait(2): raise RuntimeError('Workers did not run concurrently')
                    if data['targets'][0]['id']==1: raise RuntimeError('Authentication failed')
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline:
                        if self.cancel.exists():raise Cancelled('Sibling failed')
                        time.sleep(.01)
                    raise RuntimeError('Sibling was not cancelled')
                finally:
                    with mutex:state['active']-=1
        start=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'Authentication failed'):
            self.run_translation(Engine(), workers=4, chunk_size=2)
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(state['peak'],4)
        self.assertEqual(state['active'],0)
        self.assertFalse(self.output.exists())

    def test_invalid_worker_count_is_rejected_before_translation(self):
        for workers in [0,16,True]:
            engine=FakeEngine()
            with self.assertRaises(ValueError):self.run_translation(engine,workers=workers)
            self.assertEqual(engine.calls,[])

    def test_requested_fifteen_workers_run_concurrently_and_keep_cue_order(self):
        self.doc=parse(''.join(f'{i}\n00:00:01,000 --> 00:00:02,000\nNo.\n\n' for i in range(1,16)),'.srt')
        barrier=Barrier(15)
        class Engine(FakeEngine):
            def complete(self,data,*,review=False,**kwargs):
                if not review:barrier.wait(3)
                return super().complete(data,review=review,**kwargs)
        self.run_translation(Engine(),workers=15,chunk_size=1)
        self.assertEqual([c.id for c in read(self.output).cues],list(range(1,16)))
        self.assertEqual(json.loads(self.report.read_text())['workers'],15)


class FastTests(unittest.TestCase):
    def test_fast_service_is_passed_explicitly_to_cli(self):
        seen=[]
        def run(argv,**kwargs):
            seen.extend(argv)
            Path(argv[argv.index('--output-last-message')+1]).write_text('{"ok":true}')
            return ''
        engine=Codex(model='gpt-6.1-sol',service_tier='fast')
        with patch('codex_subs.codex.run',side_effect=run):
            self.assertEqual(engine.request('test',{'type':'object'}),{'ok':True})
        self.assertIn('service_tier="fast"',seen)
        i=seen.index('--enable')
        self.assertEqual(seen[i+1],'fast_mode')

    def test_fork_keeps_service_settings_and_has_separate_review_state(self):
        engine=Codex(model='gpt-6.1-sol',service_tier='fast',reasoning='low')
        engine.last_review={'old':True}
        cancel=Event()
        child=engine.fork(cancel)
        self.assertEqual(child.model,engine.model)
        self.assertEqual(child.service_tier,'fast')
        self.assertEqual(child.reasoning,'low')
        self.assertIs(child.cancel,cancel)
        self.assertFalse(hasattr(child,'last_review'))


if __name__=='__main__':unittest.main()

import json
from pathlib import Path
import tempfile
import unittest

from codex_subs.pipeline import ReviewRequired, translate, validated
from codex_subs.process import Cancelled, run
from codex_subs.subtitles import SubtitleError, parse


SOURCE = '1\n00:00:01,000 --> 00:00:02,000\nNo.\n\n2\n00:00:03,000 --> 00:00:04,000\nMaybe.\n'


class FakeEngine:
    model = 'test'

    def __init__(self, mode='ok'):
        self.calls = []
        self.mode = mode

    def complete(self, data, *, review=False, conservative=False):
        self.calls.append((data, review))
        rows = [{'id': c['id'], 'text': 'Нет.' if c['id'] == 1 else 'Возможно.',
                 'uncertain': review and (self.mode == 'uncertain' or self.mode == 'uncertain_then_safe' and not conservative), 'note': 'Ambiguous' if self.mode == 'uncertain' else ''}
                for c in data['targets']]
        if self.mode == 'missing':
            rows.pop()
        if self.mode == 'duplicate':
            rows += rows[:1]
        return {'cues': rows}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'movie.ru.srt'
        self.report = self.root / 'report.json'
        self.doc = parse(SOURCE, '.srt')

    def translate(self, engine=None, refs=None, **kw):
        return translate(self.doc, refs or [], engine or FakeEngine(), self.output,
                         cache=self.root / 'cache', report=self.report, **kw)

    def test_complete_pipeline_has_separate_review(self):
        engine = FakeEngine()
        self.translate(engine)
        self.assertEqual([review for _, review in engine.calls], [False, True])
        self.assertEqual(self.output.read_text(), SOURCE.replace('No.', 'Нет.').replace('Maybe.', 'Возможно.'))
        self.assertEqual(json.loads(self.report.read_text())['status'], 'complete')

    def test_bad_or_incomplete_model_output_never_published(self):
        for mode in ['missing', 'duplicate']:
            with self.subTest(mode=mode), self.assertRaises(SubtitleError):
                self.translate(FakeEngine(mode))
            self.assertFalse(self.output.exists())

    def test_unresolved_ambiguity_stops_publication(self):
        with self.assertRaises(ReviewRequired):
            self.translate(FakeEngine('uncertain'))
        self.assertFalse(self.output.exists())
        report = json.loads(self.report.read_text())
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(len(report['needs_review']), 2)

    def test_ambiguity_gets_a_conservative_review_before_stopping(self):
        engine = FakeEngine('uncertain_then_safe')
        self.translate(engine)
        self.assertEqual(len(engine.calls), 4)  # repair + independent confirmation
        self.assertTrue(self.output.exists())
        report = json.loads(self.report.read_text())
        self.assertEqual(len(report['ambiguity_reviews']), 1)

    def test_no_overwrite(self):
        self.output.write_text('existing')
        engine = FakeEngine()
        with self.assertRaises(FileExistsError):
            self.translate(engine)
        self.assertEqual(self.output.read_text(), 'existing')
        self.assertEqual(engine.calls, [])

    def test_resume_reuses_only_reviewed_chunks(self):
        self.translate(chunk_size=1)
        self.output.unlink()
        engine = FakeEngine()
        self.translate(engine, chunk_size=1)
        self.assertEqual(engine.calls, [])
        self.assertTrue(self.output.exists())

    def test_reference_changes_invalidate_checkpoint(self):
        self.translate()
        self.output.unlink()
        engine = FakeEngine()
        self.translate(engine, [('French', parse(SOURCE.replace('No.', 'Non.'), '.srt'))])
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(engine.calls[0][0]['targets'][0]['references'][0]['lines'][0]['text'], 'Non.')

    def test_references_with_bad_timing_rejected_before_model_call(self):
        ref = parse(SOURCE.replace('00:00:', '00:10:'), '.srt')
        engine = FakeEngine()
        with self.assertRaisesRegex(ValueError, 'overlaps only'):
            self.translate(engine, [('French', ref)])
        self.assertEqual(engine.calls, [])

    def test_cancel_does_not_publish(self):
        cancel = self.root / 'cancel'
        cancel.touch()
        with self.assertRaises(Cancelled):
            self.translate(cancel=cancel)
        self.assertFalse(self.output.exists())

    def test_model_schema_rejects_bool_id(self):
        with self.assertRaises(SubtitleError):
            validated({'cues': [{'id': True, 'text': 'Нет.', 'uncertain': False, 'note': ''}]}, self.doc.cues[:1])

    def test_output_suffix_cannot_silently_convert_format(self):
        self.output = self.root / 'movie.ru.ass'
        with self.assertRaisesRegex(ValueError, 'extension'):
            self.translate()

    def test_shell_metacharacters_are_literal_process_arguments(self):
        import sys
        value = "quotes' $(printf unsafe); `printf unsafe`"
        result = run([sys.executable, '-c', 'import sys; print(sys.argv[1])', value])
        self.assertEqual(result.strip(), value)

    def test_process_timeout(self):
        import sys
        with self.assertRaises(TimeoutError):
            run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.2)


if __name__ == '__main__':
    unittest.main()

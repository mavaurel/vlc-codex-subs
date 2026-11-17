import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from codex_subs.cli import source_images, reference_document
from codex_subs.layout import fit_pgs
from codex_subs.ocr import convert, suspicious, verify_image
from codex_subs.pipeline import translate
from codex_subs.subtitles import parse, read, SubtitleError
from test_pgs import fixture
from test_pipeline import FakeEngine, SOURCE


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.doc = parse(SOURCE, '.srt')
        self.output = self.root / 'Russian.srt'
        self.report = self.root / 'review.json'

    def run_translation(self, engine, **kwargs):
        return translate(self.doc, [], engine, self.output, cache=self.root/'cache',
                         report=self.report, keep_original=True, **kwargs)

    def test_unresolved_lines_keep_original_text_and_are_reported(self):
        engine = FakeEngine('uncertain')
        self.run_translation(engine)
        self.assertEqual(self.output.read_text(), SOURCE)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['status'], 'complete_with_warnings')
        self.assertEqual([r['id'] for r in report['originals_kept']], [1, 2])
        self.assertTrue(all(r['translated'] is False for r in report['originals_kept']))
        self.assertEqual(len(engine.calls), 4)

    def test_repair_is_rejected_when_independent_confirmation_remains_uncertain(self):
        class Engine(FakeEngine):
            def complete(self, data, *, review=False, conservative=False):
                response = super().complete(data, review=review, conservative=conservative)
                for row in response['cues']:
                    row['uncertain'] = review and row['id'] == 2 and (not conservative or 'verification' in data)
                    row['note'] = 'Still ambiguous' if row['uncertain'] else ''
                return response
        engine = Engine()
        self.run_translation(engine)
        self.assertEqual(self.output.read_text(), SOURCE.replace('No.', 'Нет.'))
        self.assertEqual([[c['id'] for c in data['targets']] for data, _ in engine.calls],
                         [[1, 2], [1, 2], [2], [2]])

    def test_short_thousand_cue_file_is_not_split_at_an_arbitrary_count(self):
        self.doc = parse(''.join(f'{i}\n00:00:01,000 --> 00:00:02,000\nNo.\n\n' for i in range(1, 1001)), '.srt')
        engine = FakeEngine()
        self.run_translation(engine)
        self.assertEqual([len(data['targets']) for data, _ in engine.calls], [1000, 1000])
        self.assertEqual(len(read(self.output).cues), 1000)

    def test_incomplete_large_response_splits_and_preserves_all_cues(self):
        self.doc = parse(SOURCE + '\n3\n00:00:05,000 --> 00:00:06,000\nLater.\n\n4\n00:00:07,000 --> 00:00:08,000\nStop.\n', '.srt')
        class Engine(FakeEngine):
            def complete(self, data, **kwargs):
                response = super().complete(data, **kwargs)
                if len(data['targets']) > 2:
                    response['cues'].pop()
                return response
        engine = Engine()
        self.run_translation(engine)
        out = read(self.output)
        self.assertEqual([(c.id, c.start, c.end) for c in out.cues], [(c.id, c.start, c.end) for c in self.doc.cues])
        self.assertEqual([r['size'] for r in json.loads(self.report.read_text())['split_retries']], [4])

    def test_approvals_and_original_fallback_survive_batch_size_change(self):
        self.run_translation(FakeEngine('uncertain'), chunk_size=1)
        self.output.unlink()
        engine = FakeEngine()
        self.run_translation(engine, chunk_size=0)
        self.assertEqual(engine.calls, [])
        self.assertEqual(self.output.read_text(), SOURCE)
        self.assertEqual(len(json.loads(self.report.read_text())['originals_kept']), 2)

    def test_persistently_malformed_single_response_keeps_original(self):
        self.run_translation(FakeEngine('missing'))
        self.assertEqual(self.output.read_text(), SOURCE)
        self.assertEqual(len(json.loads(self.report.read_text())['originals_kept']), 2)

    def test_timed_out_review_splits_without_retranslating_valid_draft(self):
        class Engine(FakeEngine):
            def complete(self, data, *, review=False, **kwargs):
                result = super().complete(data, review=review, **kwargs)
                if review and len(data['targets']) > 1: raise TimeoutError('Review too large')
                return result
        engine = Engine()
        self.run_translation(engine)
        self.assertEqual([(len(d['targets']), review) for d, review in engine.calls],
                         [(2, False), (2, True), (1, True), (1, True)])
        self.assertEqual(json.loads(self.report.read_text())['status'], 'complete')

    def test_original_image_requires_renderer_and_cannot_leak_placeholder(self):
        self.doc = parse(SOURCE.replace('No.', 'PGS_IMAGE_1'), '.srt')
        with self.assertRaisesRegex(ValueError, 'renderer'):
            self.run_translation(FakeEngine(), original_images={1: {}})
        with self.assertRaisesRegex(SubtitleError, 'placeholder'):
            self.run_translation(FakeEngine(), original_images={1: {}}, finish_output=lambda raw, *args: raw)
        self.assertFalse(self.output.exists())


class ImageRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root/'source.sup'
        self.source.write_bytes(fixture())

    def result(self, text='1...', confidence=1):
        return {'id': 'image', 'lines': [{'text': text, 'confidence': confidence,
                                        'x': .1, 'y': .2, 'width': .8, 'height': .5}]}

    def convert(self, verifier):
        def local_ocr(argv, stdin, **kwargs):
            rows = []
            for item in json.loads(stdin):
                result = self.result(confidence=.3)
                result['id'] = item['id']
                rows.append(json.dumps(result))
            return '\n'.join(rows)
        with patch('codex_subs.ocr.engine', return_value=Path('/fake/vision')), patch('codex_subs.ocr.run', side_effect=local_ocr):
            return convert(self.source, self.root/'generated', cache=self.root/'cache',
                           verifier=verifier, keep_original=True)

    def test_high_confidence_single_glyph_requires_two_agreeing_image_readings(self):
        result = self.result()
        self.assertTrue(suspicious(result))
        class Verifier:
            def __init__(self): self.calls = []
            def read_image(self, path):
                self.calls.append(path)
                return {'lines': ['I...'], 'uncertain': False, 'note': ''}
        verifier = Verifier()
        fixed, readings = verify_image(result, Path('image.png'), verifier)
        self.assertEqual(fixed['lines'][0]['text'], 'I...')
        self.assertEqual(len(verifier.calls), 2)
        self.assertFalse(suspicious(fixed))
        self.assertEqual(result['lines'][0]['text'], '1...')

    def test_disagreeing_or_uncertain_readings_are_never_accepted(self):
        for replies in ([{'lines': ['I...'], 'uncertain': False}, {'lines': ['1...'], 'uncertain': False}],
                        [{'lines': ['I...'], 'uncertain': True}] * 2):
            class Verifier:
                def read_image(self, path): return replies.pop(0)
            fixed, _ = verify_image(self.result(), Path('image.png'), Verifier())
            self.assertIsNone(fixed)

    def test_unreadable_image_finishes_with_original_glyphs_not_ocr_guess(self):
        class Verifier:
            def read_image(self, path): return {'lines': [], 'uncertain': True, 'note': 'Unreadable'}
        path = self.convert(Verifier())
        originals = source_images(path, self.root/'generated')
        self.assertEqual(list(originals), [1])
        self.assertEqual(reference_document(path, self.root/'generated').cues, [])
        engine = FakeEngine()
        def finish(raw, images, audit):
            return fit_pgs(raw, self.root/'cache', images, measure_fn=lambda words, *args: {w: len(w)*50 for w in words})[0]
        output = self.root/'Russian.ass'
        translate(read(path), [], engine, output, cache=self.root/'cache', report=self.root/'report.json',
                  keep_original=True, original_images=originals, finish_output=finish)
        self.assertEqual(engine.calls, [])
        text = output.read_text()
        self.assertNotIn('PGS_IMAGE_', text)
        self.assertNotIn('1...', text)
        self.assertIn(r'\p1', text)
        doc = parse(text, '.ass', allow_drawings=True)
        self.assertEqual([(c.start, c.end) for c in doc.cues], [(1, 3)])

    def test_verified_image_is_cached_and_not_sent_again(self):
        class Verifier:
            calls = 0
            def read_image(self, path):
                self.calls += 1
                return {'lines': ['I...'], 'uncertain': False, 'note': ''}
        verifier = Verifier()
        self.convert(verifier)
        self.convert(verifier)
        self.assertEqual(verifier.calls, 2)

    def test_review_ambiguity_rechecks_image_then_keeps_original_glyphs_and_resumes(self):
        class Verifier:
            def read_image(self, path): return {'lines': ['I...'], 'uncertain': False, 'note': ''}
        path = self.convert(Verifier())
        evidence = source_images(path, self.root/'generated', all_images=True)
        self.assertEqual(source_images(path, self.root/'generated'), {})
        class Engine:
            model = 'test'
            def __init__(self): self.images = []
            def complete(self, data, *, review=False, conservative=False, images=()):
                self.images.append(images)
                return {'cues': [{'id': c['id'], 'text': c['text'], 'uncertain': review, 'note': 'Unresolved'} for c in data['targets']]}
        engine = Engine()
        def finish(raw, images, audit):
            return fit_pgs(raw, self.root/'cache', images, measure_fn=lambda words, *args: {w: len(w)*50 for w in words})[0]
        output = self.root/'Russian.ass'
        def run():
            translate(read(path), [], engine, output, cache=self.root/'cache', report=self.root/'review.json',
                      keep_original=True, image_evidence=evidence, finish_output=finish)
        run()
        self.assertEqual([len(x) for x in engine.images], [0, 0, 1, 1])
        self.assertIn(r'\p1', output.read_text())
        first = output.read_bytes()
        output.unlink()
        run()
        self.assertEqual(output.read_bytes(), first)
        self.assertEqual(len(engine.images), 4)


class LayoutTests(unittest.TestCase):
    def test_long_russian_caption_fits_without_changed_dialogue_or_time(self):
        text = ('[Script Info]\nTitle: PGS OCR source\nPlayResX: 1920\nPlayResY: 960\n'
                '[V4+ Styles]\nFormat: Name, Fontname, Fontsize\nStyle: Default,Arial,70\n'
                '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                'Dialogue: 0,0:01:00.00,0:01:05.00,Default,,0,0,0,,{\\an2\\pos(1500,958)\\fs90}'
                'Можно мне и Джейден поиграть с твоими туфлями?\\N— Да, конечно.\n')
        output, metrics = fit_pgs(text.encode(), Path('.'), measure_fn=lambda words, *args: {w: len(w)*70 for w in words})
        before, after = parse(text, '.ass'), parse(output.decode(), '.ass')
        strip = lambda s: re.sub(r'\s', '', re.sub(r'\{[^}]*\}', '', s).replace(r'\N', ''))
        self.assertEqual(strip(before.cues[0].text), strip(after.cues[0].text))
        self.assertEqual((after.cues[0].start, after.cues[0].end), (60, 65))
        item = metrics[0]
        self.assertLessEqual(item['width'], item['safe_width'])
        self.assertGreaterEqual(item['left'], 1920*.07-1)
        self.assertLessEqual(item['right'], 1920*.93+1)
        self.assertGreaterEqual(item['top'], 960*.05)
        self.assertLessEqual(item['bottom'], 960*.95)
        self.assertIn(r'\N—', after.cues[0].text)

    def test_existing_ass_styles_are_not_rewritten(self):
        original = b'[Script Info]\nTitle: Existing subtitle\n'
        self.assertEqual(fit_pgs(original, Path('.')), (original, []))


if __name__ == '__main__':
    unittest.main()

import copy
import unittest

from codex_subs.codex import Codex, apply_review, schema, review_schema
from codex_subs.pipeline import validated
from codex_subs.subtitles import parse, SubtitleError


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.draft = [
            {'id': 1, 'text': 'Нет.', 'uncertain': False, 'note': ''},
            {'id': 2, 'text': 'Возможно.', 'uncertain': True, 'note': 'Unclear source'},
        ]

    def test_no_corrections_preserves_text_and_existing_uncertainty(self):
        before = copy.deepcopy(self.draft)
        result = apply_review(self.draft, {'reviewed_ids': [1, 2], 'changes': []}, [1, 2])
        self.assertEqual(result, {'cues': before})
        self.assertEqual(self.draft, before)

    def test_wire_schema_is_reused_for_different_batch_sizes_and_ids(self):
        self.assertEqual(schema([1,2]), schema(list(range(1000,1064))))
        self.assertEqual(review_schema([1,2]), review_schema(list(range(1000,1064))))

    def test_only_explicit_corrections_replace_draft_rows(self):
        change = {'id': 2, 'text': 'Может быть.', 'uncertain': False, 'note': 'Resolved from context'}
        result = apply_review(self.draft, {'reviewed_ids': [2, 1], 'changes': [change]}, [1, 2])
        self.assertEqual(result['cues'], [self.draft[0], change])
        self.assertTrue(self.draft[1]['uncertain'])

    def test_missing_duplicate_and_unexpected_review_coverage_is_rejected(self):
        for coverage in [[1], [1, 1], [1, 3], [True, 2], '1,2']:
            with self.subTest(coverage=coverage), self.assertRaises(SubtitleError):
                apply_review(self.draft, {'reviewed_ids': coverage, 'changes': []}, [1, 2])

    def test_invalid_corrections_are_rejected(self):
        row = self.draft[0]
        for changes in [[row, row], [{**row, 'id': 3}], [{**row, 'id': True}],
                        [{**row, 'uncertain': 'false'}], [{'id': 1, 'text': 'Нет.'}], {}]:
            with self.subTest(changes=changes), self.assertRaises(SubtitleError):
                apply_review(self.draft, {'reviewed_ids': [1, 2], 'changes': changes}, [1, 2])

    def test_correction_still_must_pass_source_format_validation(self):
        doc = parse('1\n00:00:01,000 --> 00:00:02,000\n<i>No.</i>\n', '.srt')
        draft = [{'id': 1, 'text': '⟦KEEP_0⟧Нет.⟦KEEP_1⟧', 'uncertain': False, 'note': ''}]
        bad = {**draft[0], 'text': 'Нет.'}
        response = apply_review(draft, {'reviewed_ids': [1], 'changes': [bad]}, [1])
        with self.assertRaises(SubtitleError): validated(response, doc.cues)

    def test_codex_review_uses_compact_schema_and_returns_complete_canonical_rows(self):
        engine = Codex(model='test')
        captured = {}
        def request(prompt, schema, images):
            captured.update(prompt=prompt, schema=schema)
            return {'reviewed_ids': [1, 2], 'changes': []}
        engine.request = request
        data = {'targets': [{'id': 1}, {'id': 2}], 'draft': self.draft}
        self.assertEqual(engine.complete(data, review=True), {'cues': self.draft})
        self.assertEqual(captured['schema']['required'], ['reviewed_ids', 'changes'])
        self.assertIn('Read and assess EVERY target cue', captured['prompt'])
        self.assertEqual(engine.last_review['reviewed_ids'], [1, 2])


if __name__ == '__main__': unittest.main()

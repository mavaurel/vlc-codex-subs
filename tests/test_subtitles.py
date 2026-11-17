import unittest

from codex_subs.subtitles import SubtitleError, align, parse


SRT = '1\r\n00:00:01,000 --> 00:00:03,000\r\n<i>No.</i>\r\n- Wait.\r\n\r\n2\r\n00:00:04,000 --> 00:00:06,000\r\nMaybe.\r\n'
ASS = r'''[Script Info]
Title: Style fixture
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,42,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,30,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Comment: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Keep this comment.
Dialogue: 0,0:00:01.00,0:00:03.00,Default,Ada,0,0,0,,{\i1}No, wait.{\i0}\NPlease.
'''


class SubtitleTests(unittest.TestCase):
    def test_srt_lossless_identity(self):
        doc = parse(SRT, ".srt")
        self.assertEqual(doc.render({c.id: c.protected()[0] for c in doc.cues}), SRT.encode())

    def test_translation_only_changes_dialogue(self):
        doc = parse(SRT, ".srt")
        result = doc.render({1: '⟦KEEP_0⟧Нет.⟦KEEP_1⟧⟦KEEP_2⟧- Подожди.', 2: 'Возможно.'}).decode()
        self.assertEqual(result, SRT.replace('No.', 'Нет.').replace('Wait.', 'Подожди.').replace('Maybe.', 'Возможно.'))

    def test_ass_style_and_metadata_are_untouched(self):
        doc = parse(ASS, ".ass")
        result = doc.render({1: '⟦KEEP_0⟧Нет, подожди.⟦KEEP_1⟧⟦KEEP_2⟧Пожалуйста.'}).decode()
        self.assertEqual(result, ASS.replace('No, wait.', 'Нет, подожди.').replace('Please.', 'Пожалуйста.'))
        self.assertEqual(doc.cues[0].speaker, 'Ada')

    def test_vtt_notes_styles_ids_settings(self):
        source = 'WEBVTT\n\nSTYLE\n::cue { color: lime; }\n\nNOTE source\nkeep this\n\ncue-a\n00:01.000 --> 00:03.000 position:10%\n<v Ada><i>Wait.</i></v>\n'
        doc = parse(source, ".vtt")
        result = doc.render({1: '⟦KEEP_0⟧⟦KEEP_1⟧Подожди.⟦KEEP_2⟧⟦KEEP_3⟧'}).decode()
        self.assertEqual(result, source.replace('Wait.', 'Подожди.'))

    def test_overlapping_and_nonchronological_cues_preserve_order(self):
        doc = parse(SRT.replace('00:00:04,000', '00:00:00,000'), '.srt')
        self.assertEqual(doc.render({c.id: c.protected()[0] for c in doc.cues}), doc.text.encode())

    def test_missing_format_marker_rejected(self):
        with self.assertRaises(SubtitleError):
            parse(SRT, '.srt').cues[0].restore('Нет.')

    def test_added_markup_newline_and_control_rejected(self):
        cue = parse(SRT, '.srt').cues[1]
        for text in ['<b>Да.</b>', 'Да.\nПривет.', 'Да.\x00', 'Да.\u2028Привет.', '00:00:01 --> 00:00:02']:
            with self.subTest(text=text), self.assertRaises(SubtitleError):
                cue.restore(text)

    def test_dropped_and_invented_segments_rejected(self):
        cue = parse(SRT, '.srt').cues[0]
        for text in ['⟦KEEP_0⟧⟦KEEP_1⟧⟦KEEP_2⟧- Подожди.',
                     'Привет.⟦KEEP_0⟧Нет.⟦KEEP_1⟧⟦KEEP_2⟧- Подожди.']:
            with self.subTest(text=text), self.assertRaises(SubtitleError):
                cue.restore(text)

    def test_reordered_markers_rejected(self):
        with self.assertRaises(SubtitleError):
            parse(SRT, '.srt').cues[0].restore('⟦KEEP_1⟧Нет.⟦KEEP_0⟧⟦KEEP_2⟧- Подожди.')

    def test_extra_and_missing_cues_rejected(self):
        doc = parse(SRT, '.srt')
        for values in [{1: 'Нет.'}, {1: 'Нет.', 2: 'Да.', 3: 'Привет.'}]:
            with self.assertRaises(SubtitleError):
                doc.render(values)

    def test_invalid_input_rejected(self):
        for source, suffix in [('hello', '.srt'), ('1\n00:00:02,000 --> 00:00:01,000\nNo.', '.srt'),
                               ('00:01.000 --> 00:02.000\nNo.', '.vtt'), ('text', '.xml')]:
            with self.subTest(source=source), self.assertRaises(SubtitleError):
                parse(source, suffix)

    def test_drawings_and_karaoke_rejected(self):
        for tag in [r'{\p1}', r'{\k20}', r'{\kf20}', r'{\K20}']:
            with self.subTest(tag=tag), self.assertRaises(SubtitleError):
                parse(ASS.replace(r'{\i1}', tag), '.ass')

    def test_multilingual_alignment_by_time_not_id(self):
        primary = parse(SRT, '.srt')
        ref = parse('20\n00:00:01,300 --> 00:00:02,500\nNon.\n\n99\n00:00:04,000 --> 00:00:06,000\nPeut-être.\n', '.srt')
        aligned = align(primary, ref)
        self.assertEqual(aligned[1][0].text, 'Non.')
        self.assertEqual(aligned[2][0].text, 'Peut-être.')

    def test_segmentation_can_differ(self):
        primary = parse(SRT, '.srt')
        ref = parse('1\n00:00:01,000 --> 00:00:06,000\nNo. Wait. Maybe.\n', '.srt')
        self.assertEqual([len(v) for v in align(primary, ref).values()], [1, 1])


if __name__ == '__main__':
    unittest.main()

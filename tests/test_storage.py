import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from codex_subs.storage import media_key,remember_job,backfill_history,preserve_result


class StorageTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.job=self.root/'jobs/100-1';self.job.mkdir(parents=True)
        self.video=self.root/'episode.mkv';self.video.write_bytes(b'media')
        self.output=self.job/'Russian.srt';self.output.write_bytes(b'complete subtitles')
        (self.job/'review.json').write_text('{}')

    def test_completed_copy_remains_beside_video_and_in_job(self):
        result=preserve_result(self.job,self.video,self.output)
        self.assertEqual(Path(result['subtitle']),self.root/'episode.ru.srt')
        self.assertEqual(Path(result['subtitle']).read_bytes(),self.output.read_bytes())
        self.assertEqual(result['warning'],'')
        self.assertEqual(json.loads((self.job/'result.json').read_text()),result)

    def test_user_subtitles_are_never_overwritten(self):
        original=self.root/'episode.ru.srt';original.write_bytes(b'user translation')
        result=preserve_result(self.job,self.video,self.output)
        self.assertEqual(original.read_bytes(),b'user translation')
        self.assertEqual(Path(result['subtitle']).name,'episode.codex.ru.srt')

    def test_identical_existing_result_is_reused(self):
        first=preserve_result(self.job,self.video,self.output)
        second=preserve_result(self.job,self.video,self.output)
        self.assertEqual(first,second)
        self.assertFalse((self.root/'episode.codex.ru.srt').exists())

    def test_unwritable_video_folder_keeps_completed_job_usable(self):
        with patch('codex_subs.storage.publish',side_effect=PermissionError('Read-only')):
            result=preserve_result(self.job,self.video,self.output)
        self.assertEqual(result['subtitle'],str(self.output))
        self.assertTrue(self.output.exists())
        self.assertIn('saved job',result['warning'])

    def test_video_index_keeps_newest_translation_and_validates_uri(self):
        uri='file:///media/%D1%81%D0%B5%D1%80%D0%B8%D1%8F.mkv'
        remember_job(self.root,uri,'200-1');remember_job(self.root,uri,'100-1')
        lines=(self.root/f'video-{media_key(uri)}.txt').read_text().splitlines()
        self.assertEqual(lines[0],'200-1');self.assertEqual(unquote(lines[1]),uri)
        other='file:///other.mkv';remember_job(self.root,other,'300-1')
        self.assertNotEqual(media_key(uri),media_key(other))
        self.assertTrue((self.root/f'video-{media_key(uri)}.txt').exists())

    def test_backfill_ignores_newer_discovery_and_malformed_jobs(self):
        uri=self.video.as_uri()
        (self.job/'request.json').write_text(json.dumps({'action':'translate','media':uri}))
        scan=self.root/'jobs/200-1';scan.mkdir();(scan/'request.json').write_text(json.dumps({'action':'discover','media':uri}))
        malformed=self.root/'jobs/300-1';malformed.mkdir();(malformed/'request.json').write_text('[]')
        backfill_history(self.root)
        self.assertEqual((self.root/f'video-{media_key(uri)}.txt').read_text().splitlines()[0],'100-1')


if __name__=='__main__':unittest.main()

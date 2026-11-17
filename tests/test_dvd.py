import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_subs import dvd
from codex_subs.media import local_path, discover
from codex_subs.cli import resolve_source, worker
from codex_subs.pgs import PGSError
from codex_subs.process import Cancelled
from codex_subs.storage import preserve_result
from codex_subs.ocr import convert
from codex_subs.subtitles import read
from setup_dvd import fix_dvd_discontinuities


class DVDTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.movie = self.root/'Film with spaces'
        self.video_ts = self.movie/'VIDEO_TS'; self.video_ts.mkdir(parents=True)
        (self.video_ts/'VIDEO_TS.IFO').write_bytes(b'IFO')
        self.titles = [dict(title=1,duration=2,streams=[]),
                       dict(title=2,duration=7900,width=720,height=576,
                            streams=[dict(index=7,id=32,language='eng'),dict(index=8,id=33,language='fre')]),
                       dict(title=3,duration=9390,width=720,height=576,streams=[dict(index=7,id=32,language='eng')])]

    def test_local_dvd_mrl_directory_and_title(self):
        for uri in [str(self.movie),str(self.video_ts),self.movie.as_uri(),
                    'dvdnav://'+str(self.video_ts)+'#codex-title=2',
                    'dvd://'+str(self.movie)+'#3:1:1']:
            self.assertEqual(local_path(uri),self.movie)
        self.assertEqual(dvd.requested_title('dvdnav:///movie#codex-title=2'),2)
        self.assertEqual(dvd.requested_title('dvd:///movie#3:1:1'),3)
        self.assertIsNone(dvd.requested_title('dvdnav:///movie#0'))
        for uri in ['https://example.org/movie','dvdnav://server/share','file://server/movie',str(self.root)]:
            with self.assertRaises(ValueError): local_path(uri)
        with self.assertRaises(ValueError): dvd.requested_title('dvd:///movie#100')

    def test_discovery_respects_selected_title_and_languages(self):
        with patch('codex_subs.dvd.scan', return_value=self.titles):
            choices,warnings = discover(self.movie,dvd_title=2)
            self.assertEqual([c['value'] for c in choices],['2:7','2:8'])
            self.assertTrue(all(c['kind']=='dvd_track' for c in choices))
            self.assertIn('English',choices[0]['label']); self.assertIn('French',choices[1]['label'])
            self.assertEqual(warnings,[])
            choices,warnings = discover(self.movie)
            self.assertEqual(choices[0]['value'],'3:7'); self.assertTrue(warnings)
            with self.assertRaises(ValueError): discover(self.movie,dvd_title=4)

    def bitmap_manifest(self):
        raw=self.root/'raw'; raw.mkdir()
        (raw/'1.gray').write_bytes(b'\0'*24)
        manifest=raw/'displays.json'
        data=dict(version=1,width=720,height=576,displays=[dict(start=135000,end=360000,
            parts=[dict(x=300,y=450,width=8,height=3,file='1.gray',forced=False)])])
        manifest.write_text(json.dumps(data))
        return manifest,data

    def test_bitmap_reader_preserves_timing_position_and_cancellation(self):
        manifest,data=self.bitmap_manifest()
        display=list(dvd.displays(manifest))[0]
        self.assertEqual((display.start,display.end),(135000,360000))
        self.assertEqual((display.parts[0].x,display.parts[0].y),(300,450))
        self.assertTrue(display.parts[0].image.startswith(b'\x89PNG'))
        cancel=self.root/'cancel'; cancel.touch()
        with self.assertRaises(Cancelled): list(dvd.displays(manifest,cancel))
        for mutate in [lambda d:d['displays'][0].update(end=100),
                       lambda d:d['displays'][0]['parts'][0].update(file='../outside.gray'),
                       lambda d:d['displays'][0]['parts'][0].update(width=700)]:
            damaged=json.loads(json.dumps(data)); mutate(damaged); manifest.write_text(json.dumps(damaged))
            with self.assertRaises(PGSError): list(dvd.displays(manifest))

    def test_dvd_ocr_uses_existing_reviewable_ass_pipeline(self):
        manifest,data=self.bitmap_manifest()
        def recognize(argv,**kwargs):
            return '\n'.join(json.dumps({'id':i['id'],'lines':[{'text':'Hello, New York!','confidence':.99,
                'x':.1,'y':.2,'width':.7,'height':.5}]}) for i in json.loads(kwargs['stdin']))
        cache=self.root/'cache'; cache.mkdir()
        with patch('codex_subs.ocr.engine',return_value=Path('/vision-test')), patch('codex_subs.ocr.run',side_effect=recognize):
            source=convert(manifest,self.root/'ocr',cache=cache,language='eng',
                           display_reader=dvd.displays,source_format='DVD')
        document=read(source)
        self.assertEqual(len(document.cues),1)
        self.assertIn('Hello, New York!',document.cues[0].text)
        self.assertEqual(json.loads((source.parent/'ocr.json').read_text())['source_format'],'DVD')
        self.assertIn('0:00:01.50,0:00:04.00',source.read_text())

    def test_completed_dvd_output_does_not_touch_video_ts(self):
        job=self.root/'job'; job.mkdir(); output=job/'Russian.ass'; output.write_text('subtitles')
        result=preserve_result(job,self.movie,output,dvd_title=2)
        self.assertEqual(Path(result['subtitle']),self.movie/'Film with spaces.title-2.ru.ass')
        self.assertEqual(list(self.video_ts.iterdir()),[self.video_ts/'VIDEO_TS.IFO'])
        output.write_text('another translation')
        second=preserve_result(job,self.movie,output,dvd_title=2)
        self.assertNotEqual(result['subtitle'],second['subtitle'])
        self.assertEqual(Path(result['subtitle']).read_text(),'subtitles')

    def test_worker_dvd_discovery_routes_to_native_reader(self):
        job=self.root/'jobs/100-1'; job.mkdir(parents=True)
        (job/'request.json').write_text(json.dumps({'action':'discover','media':'dvdnav://'+str(self.video_ts)+'#codex-title=2'}))
        with patch('codex_subs.dvd.scan',return_value=self.titles): worker(job,{})
        self.assertIn('dvd_track', (job/'choices.tsv').read_text())
        self.assertIn('2%3A7', (job/'choices.tsv').read_text())
        self.assertEqual((job/'discovery-version.txt').read_text(),'1.3\n')

    def test_dvd_chapter_skip_uses_signed_timeline_offset(self):
        source=self.root/'ffmpeg/libavformat'; source.mkdir(parents=True)
        code=source/'dvdvideodec.c'
        code.write_text('state->ptm_offset  += state->vobu_e_ptm - next;\n'*2)
        fix_dvd_discontinuities(source.parent)
        self.assertEqual(code.read_text().count('(int64_t)state->vobu_e_ptm'),2)
        fix_dvd_discontinuities(source.parent) # Rebuilding is idempotent.
        code.write_text('changed upstream')
        with self.assertRaises(RuntimeError): fix_dvd_discontinuities(source.parent)


if __name__=='__main__': unittest.main()

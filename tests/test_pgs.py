import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from codex_subs.pgs import PGSError, Bitmap, Display, decode_rle, displays, extract_sup, png
from codex_subs.ocr import convert, ass_time
from codex_subs.pipeline import ReviewRequired
from codex_subs.process import Cancelled
from codex_subs.media import discover
from codex_subs.subtitles import read


def segment(kind,body,pts):
    return struct.pack('>2sIIBH',b'PG',pts,pts,kind,len(body))+body


def presentation(pts,objects=1,state=128,crop=None):
    body = struct.pack('>HHBHBBBB',1920,1080,16,1,state,0,0,objects)
    if objects:
        body += struct.pack('>HBBHH',0,0,128 if crop else 0,400,900)
        if crop: body += struct.pack('>HHHH',*crop)
    return segment(22,body,pts)


def fixture(*,fragments=False,crop=None):
    pts=90000
    rle=b'\x01\x01\x00\x00\x01\x01\x00\x00'
    header = struct.pack('>HBB',0,0,128 if fragments else 192)+(len(rle)+4).to_bytes(3,'big')+struct.pack('>HH',2,2)
    obj=segment(21,header+(rle[:4] if fragments else rle),pts)
    if fragments: obj+=segment(21,struct.pack('>HBB',0,0,64)+rle[4:],pts)
    return (presentation(pts,crop=crop)+segment(20,bytes([0,0,0,16,128,128,0,1,235,128,128,255]),pts)
            +obj+segment(128,b'',pts)+presentation(270000,0,0)+segment(128,b'',270000))


class PGSTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.source=self.root/'test.sup'

    def decode(self,data):
        self.source.write_bytes(data); return list(displays(self.source))

    def test_real_run_length_forms_and_exact_rows(self):
        self.assertEqual(decode_rle(b'\x01\x00\x82\x02\x00\x01\x00\x00',4,1),bytes([1,2,2,0]))
        self.assertEqual(decode_rle(b'\x00\x41\x00\x00\x00',256,1),bytes(256))
        for broken in [b'\x01\x00',b'\x01\x00\x00',b'\x00\x83\x01\x00\x00']:
            with self.assertRaises(PGSError): decode_rle(broken,2,1)

    def test_fragmented_objects_keep_original_times_and_position(self):
        for fragments in (False,True):
            d=self.decode(fixture(fragments=fragments))[0]
            self.assertEqual((d.start,d.end,d.width,d.height),(90000,270000,1920,1080))
            b=d.parts[0]; self.assertEqual((b.x,b.y,b.width,b.height),(400,900,2,2))
            self.assertTrue(b.image.startswith(b'\x89PNG'))

    def test_crop_applies_to_bitmap(self):
        b=self.decode(fixture(crop=(1,0,1,2)))[0].parts[0]
        self.assertEqual((b.width,b.height),(1,2))

    def test_repeated_display_does_not_duplicate_dialogue(self):
        data=fixture(); extra=presentation(180000,1,0)+segment(128,b'',180000)
        at=data.index(b'PG'+(270000).to_bytes(4,'big'))
        ds=self.decode(data[:at]+extra+data[at:]); self.assertEqual(len(ds),1)
        self.assertEqual(ds[0].end,270000)

    def test_stale_clear_at_new_cue_start_keeps_new_caption(self):
        data=fixture(); at=data.index(b'PG'+(270000).to_bytes(4,'big'))
        data=data[:at]+presentation(90000,0,0)+segment(128,b'',90000)+data[at:]
        self.assertEqual(len(self.decode(data)),1)

    def test_incomplete_stream_never_guesses_end_or_drops_pixels(self):
        for data in [fixture()[:-13],fixture()[:-1],b'bad',fixture(crop=(2,0,1,1))]:
            with self.assertRaises(PGSError): self.decode(data)

    def test_cancel_during_decode(self):
        self.source.write_bytes(fixture()); cancel=self.root/'cancel'; cancel.touch()
        with self.assertRaises(Cancelled): list(displays(self.source,cancel))

    def test_extraction_preserves_and_sorts_original_nonmonotonic_pts(self):
        target=self.root/'result.sup'
        late=b'\x80\x00\x00'; early=b'\x14\x00\x02\x00\x00'
        def fake_run(argv,**kw):
            if '-show_packets' in argv:
                return json.dumps({'packets':[{'pts_time':'2.001','size':str(len(late))},{'pts_time':'1.123','size':str(len(early))}]})
            Path(argv[-1]).write_bytes(late+early); return ''
        with patch('codex_subs.pgs.run',side_effect=fake_run): extract_sup('movie',3,target,'ffmpeg','ffprobe')
        data=target.read_bytes()
        self.assertEqual(int.from_bytes(data[2:6],'big'),101070)
        second=13+2; self.assertEqual(int.from_bytes(data[second+2:second+6],'big'),180090)
        self.assertFalse(target.with_suffix('.raw').exists())

    def test_pgs_tracks_are_selectable_and_short_track_is_not_default(self):
        self.source=self.root/'video.mkv'; self.source.write_bytes(b'video')
        streams=[{'index':i+2,'codec_name':'hdmv_pgs_subtitle','tags':{'language':'eng','NUMBER_OF_FRAMES':str(n)},'disposition':{'default':i==0}} for i,n in enumerate([14,1256,1606])]
        with patch('codex_subs.media.probe',return_value=streams),patch('codex_subs.media.sys.platform','darwin'):
            choices,warnings=discover(self.source)
        self.assertEqual([c['value'] for c in choices],['3','4','2'])
        self.assertIn('Track 2:',choices[0]['label']);self.assertIn('PGS (OCR)',choices[0]['label'])
        self.assertIn('short track',choices[-1]['label']); self.assertEqual(warnings,[])


class OCRTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'test.sup';self.source.write_bytes(fixture())
        self.confidence=1; self.text='Hello.'; self.calls=0

    def fake_run(self,argv,stdin,**kw):
        self.calls+=1
        return '\n'.join(json.dumps({'id':i['id'],'lines':[{'text':self.text,'confidence':self.confidence,
            'x':.1,'y':.2,'width':.8,'height':.5}]}) for i in json.loads(stdin))

    def convert(self,root='output'):
        with patch('codex_subs.ocr.engine',return_value=Path('/fake/vision')),patch('codex_subs.ocr.run',side_effect=self.fake_run):
            return convert(self.source,self.root/root,cache=self.root/'cache',language='eng')

    def test_ocr_generates_valid_positioned_ass_with_original_timing(self):
        output=self.convert();doc=read(output)
        self.assertEqual((doc.cues[0].start,doc.cues[0].end),(1,3))
        self.assertIn(r'\pos(401,',doc.cues[0].text)
        self.assertTrue(doc.cues[0].text.endswith('Hello.'))
        self.assertEqual(json.loads((output.parent/'ocr.json').read_text())['status'],'complete')

    def test_uncertain_ocr_stops_before_translation_and_saves_evidence(self):
        self.confidence=.3
        with self.assertRaises(ReviewRequired): self.convert()
        self.assertFalse((self.root/'output/source.ass').exists())
        report=json.loads((self.root/'output/ocr.json').read_text());self.assertEqual(report['needs_review'],[1])
        self.assertTrue(Path(report['cues'][0]['image']).exists())

    def test_ocr_cannot_inject_ass_control_tags(self):
        self.text=r'{\p1}bad'
        with self.assertRaises(ReviewRequired): self.convert()

    def test_ocr_cache_reuses_only_confident_results(self):
        self.convert();self.convert('second');self.assertEqual(self.calls,1)
        self.assertEqual(ass_time(123456),'0:00:01.37')

    def test_missing_ocr_image_response_is_rejected(self):
        with patch('codex_subs.ocr.engine',return_value=Path('/fake/vision')),patch('codex_subs.ocr.run',return_value=''):
            with self.assertRaisesRegex(RuntimeError,'missing or duplicate'):
                convert(self.source,self.root/'output',cache=self.root/'cache')


if __name__=='__main__': unittest.main()

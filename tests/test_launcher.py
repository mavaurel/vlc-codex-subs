import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipIf(os.name == 'nt', 'POSIX launcher integration')
class LauncherTests(unittest.TestCase):
    def test_translation_detaches_and_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / 'jobs' / '1-1'
            job.mkdir(parents=True)
            source = root / 'source.srt'
            source.write_text('1\n00:00:01,000 --> 00:00:02,000\nTest.\n')
            marker = root / 'model-started'
            fake = root / 'fake-codex'
            fake.write_text('#!' + sys.executable + '\nimport time\nfrom pathlib import Path\n' +
                            f'Path({str(marker)!r}).touch()\ntime.sleep(30)\n')
            fake.chmod(0o755)
            config = root / 'config.json'
            config.write_text(json.dumps({'codex':str(fake),'model':'test'}))
            (job/'request.json').write_text(json.dumps({'action':'translate','media':'',
                'primary':{'kind':'file','value':str(source)},'references':[]}))
            project = Path(__file__).resolve().parents[1]
            subprocess.run([sys.executable,'-m','codex_subs','start-job',str(job),
                            '--config',str(config),'--wait','0'],cwd=project,check=True,timeout=3)
            try:
                deadline=time.monotonic()+5
                while not marker.exists() and time.monotonic()<deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists(), (job/'worker.log').read_text())
            finally:
                subprocess.run([sys.executable,'-m','codex_subs','cancel-job',str(job)],
                               cwd=project,check=True,timeout=4)
                deadline=time.monotonic()+5
                state=''
                while time.monotonic()<deadline:
                    status=job/'status.txt'
                    if status.exists():
                        state=status.read_text().splitlines()[1]
                        if state in ('cancelled','error','done'):
                            break
                    time.sleep(.05)
                self.assertEqual(state,'cancelled')

    def test_cancel_abandoned_ocr_resets_status_and_keeps_cache(self):
        from codex_subs.cli import cancel_job, write_status
        with tempfile.TemporaryDirectory(prefix='cancel spaced ') as directory:
            job = Path(directory)
            (job/'request.json').write_text(json.dumps({'media':'file:///movie.mkv'}))
            (job/'ocr.json').write_text('saved OCR')
            write_status(job,'ocr',1380,1538,'Reading images')
            cancel_job(job)
            self.assertEqual((job/'status.txt').read_text().splitlines()[1], 'cancelled')
            self.assertEqual((job/'ocr.json').read_text(), 'saved OCR')

    def test_cancel_during_preprocessing_terminates_child(self):
        from unittest.mock import patch
        from codex_subs.cli import worker, cancel_job
        from codex_subs.process import run
        import threading
        with tempfile.TemporaryDirectory(prefix='cancel spaced ') as directory:
            job=Path(directory)
            (job/'request.json').write_text(json.dumps({'action':'translate','media':'',
                'primary':{'kind':'file','value':'unused'}}))
            started=threading.Event()
            def preprocessing(*args, **kwargs):
                started.set()
                return run([sys.executable,'-c','import time; time.sleep(30)'], cancel=job/'cancel')
            with patch('codex_subs.cli.resolve_source', side_effect=preprocessing):
                thread=threading.Thread(target=worker,args=(job,{'model':'test'}))
                thread.start()
                self.assertTrue(started.wait(3), (job/'status.txt').read_text())
                (job/'cancel').touch()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual((job/'status.txt').read_text().splitlines()[1], 'cancelled')

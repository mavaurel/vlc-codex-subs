"""Optional Lua 5.1 UI-contract tests: pip install lupa (test-only dependency)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import quote

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


@unittest.skipIf(LuaRuntime is None, 'Optional test dependency: lupa')
class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vlc-lua-'quote-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'jobs').mkdir()
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().make_dir = self.make_dir
        self.lua.execute(r'''
widgets = {}; commands = {}; loads = {}; current_uri = ''; native_execute = os.execute
function widget(text)
    local w = {text=text, values={}, selection={}, selected=0}
    function w:set_text(value) self.text=value end
    function w:get_text() return self.text end
    function w:add_value(label, id) if next(self.values)==nil then self.selected=id end; self.values[id]=label end
    function w:get_value() return self.selected end
    function w:get_selection() return self.selection end
    function w:clear() self.values={}; self.selection={}; self.selected=0 end
    table.insert(widgets,w); return w
end
vlc = {io={mkdir=function(path, mode) return make_dir(path) end},
       input={item=function() if current_uri=='' then return nil end; return {uri=function() return current_uri end} end,
              add_subtitle=function(path, select) table.insert(loads, {path,select}) end},
       deactivate=function() deactivate() end}
function vlc.dialog(title)
    local d={}
    function d:add_label(text, ...) return widget(text) end
    function d:add_button(text, fn, ...) local w=widget(text); w.callback=fn; return w end
    function d:add_text_input(text, ...) return widget(text) end
    function d:add_dropdown(...) return widget('dropdown') end
    function d:add_list(...) return widget('list') end
    function d:show() end
    function d:update() end
    function d:delete() end
    return d
end
os.execute=function(command) table.insert(commands,command); return 0 end
''')
        script = (Path(__file__).parents[1] / 'extension.lua').read_text()
        for key, value in {'PYTHON': sys.executable, 'APP': self.root, 'DATA': self.root, 'CONFIG': self.root / 'config.json'}.items():
            script = script.replace('@' + key + '@', json.dumps(str(value)))
        self.lua.execute(script)
        self.lua.globals().activate()
        self.lua.globals().current_uri = 'file:///movie.mkv'

    def make_dir(self, path):
        try:
            Path(path).mkdir()
            return 0
        except FileExistsError:
            return -1

    def widget(self, text, occurrence=0):
        return [v for v in self.lua.globals().widgets.values() if v['text'] == text][occurrence]

    def latest_job(self):
        return sorted((self.root / 'jobs').iterdir())[-1]

    def status(self, job, state, *, output='', media='file:///movie.mkv'):
        values = ['1', state, '2', '2', 'Test status', output, media]
        (job / 'status.txt').write_text('\n'.join(quote(v, safe='') for v in values) + '\n')
        if state == 'discovered': (job/'discovery-version.txt').write_text('1.3\n')

    def scan(self):
        self.lua.globals().scan_sources()
        job = self.latest_job()
        rows = [('track','1','French'), ('track','2','English'), ('file',str(self.root / 'Español.srt'),'Spanish')]
        (job / 'choices.tsv').write_text('\n'.join('\t'.join(quote(v, safe='') for v in row) for row in rows))
        self.status(job, 'discovered', media=self.lua.globals().current_uri)
        self.lua.globals().check_progress()
        return job

    def test_scan_launch_uses_only_available_extension_apis(self):
        self.lua.globals().scan_sources()
        req = json.loads((self.latest_job() / 'request.json').read_text())
        self.assertEqual(req['action'], 'discover')
        self.assertEqual(req['media'], 'file:///movie.mkv')
        command = self.lua.globals().commands[1]
        self.assertIn("'\\''", command)  # Paths containing apostrophes are shell-escaped.
        self.assertIn(' -m codex_subs start-job ', command)

    def test_dvd_title_is_part_of_job_identity_and_source_selection(self):
        self.lua.execute('current_title=2; vlc.object={input=function() return {} end}; vlc.var={get=function() return current_title end}')
        self.lua.globals().current_uri='dvdnav:///Movie/VIDEO_TS'
        self.lua.globals().scan_sources()
        job=self.latest_job()
        req=json.loads((job/'request.json').read_text())
        self.assertEqual(req['media'],'dvdnav:///Movie/VIDEO_TS#codex-title=2')
        (job/'choices.tsv').write_text('dvd_track\t2%3A7\tEnglish%20DVD%20%28OCR%29')
        self.status(job,'discovered',media=req['media'])
        self.lua.globals().check_progress()
        self.lua.globals().translate_subtitles()
        translation=json.loads((self.latest_job()/'request.json').read_text())
        self.assertEqual(translation['primary']['kind'],'dvd_track')
        self.assertEqual(translation['primary']['value'],'2:7')
        self.lua.globals().current_title=3
        self.lua.globals().input_changed()
        self.assertEqual(json.loads((self.latest_job()/'request.json').read_text())['media'],
                         'dvdnav:///Movie/VIDEO_TS#codex-title=3')

    def test_added_references_are_written_and_primary_excluded(self):
        self.scan()
        self.widget('dropdown')['selected'] = 1
        for index in [1, 2, 3, 2]:
            self.widget('dropdown', 1)['selected'] = index
            self.lua.globals().add_reference()
        self.lua.globals().translate_subtitles()
        req = json.loads((self.latest_job() / 'request.json').read_text())
        self.assertEqual(req['primary']['value'], '1')
        self.assertEqual({r['label'] for r in req['references']}, {'English', 'Spanish'})

    def test_completed_subtitles_loaded_once_only_for_same_media(self):
        self.scan()
        self.lua.globals().translate_subtitles()
        self.status(self.latest_job(), 'done', output='/tmp/Russian.ass')
        self.lua.globals().check_progress()
        self.lua.globals().check_progress()
        self.assertEqual(len(self.lua.globals().loads), 1)
        self.assertEqual(self.lua.globals().loads[1][1], '/tmp/Russian.ass')
        self.assertTrue(self.lua.globals().loads[1][2])

    def test_new_video_does_not_receive_old_translation(self):
        self.scan()
        self.lua.globals().translate_subtitles()
        self.status(self.latest_job(), 'done', output='/tmp/Russian.ass')
        self.lua.globals().current_uri = 'file:///different.mkv'
        self.lua.globals().check_progress()
        self.assertEqual(len(self.lua.globals().loads), 0)

    def test_default_light_and_explicit_careful_mode_reach_worker(self):
        self.scan()
        self.lua.globals().translate_subtitles()
        self.assertEqual(json.loads((self.latest_job()/'request.json').read_text())['profile'], 'light')
        self.status(self.latest_job(), 'cancelled')
        self.lua.globals().show_settings()
        self.widget('dropdown', 2)['selected'] = 2
        self.lua.globals().translate_subtitles()
        self.assertEqual(json.loads((self.latest_job()/'request.json').read_text())['profile'], 'careful')

    def test_original_caption_warning_remains_after_repeated_refresh(self):
        self.scan()
        self.lua.globals().translate_subtitles()
        job = self.latest_job()
        self.status(job, 'done', output='/tmp/Russian.ass')
        p = job/'status.txt'
        p.write_text(p.read_text().replace('Test%20status', quote('Complete. 1 original caption kept untranslated; see review.json', safe='')))
        for _ in range(2):
            self.lua.globals().check_progress()
            self.assertTrue(any('kept untranslated' in w['text'].replace('<br/>', '') for w in self.lua.globals().widgets.values()))

    def test_busy_guard_and_cancel(self):
        self.lua.globals().scan_sources()
        self.lua.globals().scan_sources()
        self.assertEqual(len(self.lua.globals().commands), 1)
        self.lua.globals().cancel_job()
        self.assertTrue((self.latest_job() / 'cancel').exists())

    def test_optional_reference_starts_with_none(self):
        self.scan()
        self.assertEqual(self.widget('dropdown', 1)['selected'], 0)
        self.lua.globals().add_reference()
        self.lua.globals().translate_subtitles()
        req = json.loads((self.latest_job() / 'request.json').read_text())
        self.assertEqual(req['references'], [])

    def test_vlc_poll_is_never_used_as_a_timer(self):
        self.lua.execute('poll_called=false; vlc.net={poll=function() poll_called=true; error("poll has no timeout") end}')
        self.lua.globals().scan_sources()
        self.assertFalse(self.lua.globals().poll_called)

    def test_running_job_survives_dialog_reopen(self):
        self.scan()
        self.widget('dropdown')['selected'] = 2
        self.lua.globals().translate_subtitles()
        self.status(self.latest_job(), 'reviewing')
        self.lua.globals().deactivate()
        self.lua.globals().widgets = self.lua.table()
        self.lua.globals().activate()
        self.assertEqual(self.widget('dropdown')['values'][1], 'English')
        self.assertEqual(len(self.lua.globals().commands), 2)

    def test_media_change_resets_track_selections(self):
        self.scan()
        self.lua.globals().current_uri = 'file:///different.mkv'
        self.lua.globals().input_changed()
        self.assertEqual(len(self.widget('dropdown')['values']), 0)
        self.assertEqual(len(self.widget('dropdown', 1)['values']), 0)

    def test_same_media_event_keeps_discovered_sources(self):
        self.scan()
        self.lua.globals().input_changed()
        self.assertEqual(len(self.widget('dropdown')['values']), 3)

    def test_saved_translation_survives_later_discovery_for_same_video(self):
        self.scan();self.lua.globals().translate_subtitles()
        translated=self.latest_job()
        self.status(translated,'done',output='/tmp/movie.ru.srt')
        self.scan()  # A newer discovery used to hide the completed translation.
        before=len(self.lua.globals().commands)
        self.lua.globals().deactivate();self.lua.globals().widgets=self.lua.table()
        self.lua.globals().activate()
        self.assertEqual(len(self.lua.globals().commands),before)
        self.assertEqual(self.lua.globals().loads[1][1],'/tmp/movie.ru.srt')

    def test_switching_back_recovers_running_job_without_duplicate_or_cancellation(self):
        self.scan();self.lua.globals().translate_subtitles()
        original=self.latest_job();self.status(original,'reviewing')
        self.lua.globals().current_uri='file:///other.mkv';self.lua.globals().input_changed()
        other=self.latest_job();self.status(other,'discovered',media='file:///other.mkv')
        self.assertFalse((original/'cancel').exists())
        before=len(self.lua.globals().commands)
        self.lua.globals().current_uri='file:///movie.mkv';self.lua.globals().input_changed()
        self.lua.globals().translate_subtitles()
        self.assertEqual(len(self.lua.globals().commands),before)
        self.assertFalse((original/'cancel').exists())
        self.assertEqual(len(self.lua.globals().loads),0)

    def test_video_index_recovers_completed_translation_after_switch(self):
        self.scan();self.lua.globals().translate_subtitles()
        original=self.latest_job();self.status(original,'done',output='/tmp/movie.ru.srt')
        self.lua.globals().current_uri='file:///other.mkv';self.lua.globals().input_changed()
        other=self.latest_job();self.status(other,'discovered',media='file:///other.mkv')
        self.lua.globals().current_uri='file:///movie.mkv';self.lua.globals().input_changed()
        self.assertEqual(len(self.lua.globals().loads),1)
        self.assertEqual(self.lua.globals().loads[1][1],'/tmp/movie.ru.srt')

    def test_unicode_video_hash_matches_python_history_writer(self):
        from codex_subs.storage import media_key
        uri='file:///tmp/серия one.mkv'
        self.lua.globals().current_uri=uri
        self.scan();self.lua.globals().translate_subtitles()
        pointer=self.root/f'video-{media_key(uri)}.txt'
        self.assertTrue(pointer.exists())
        from urllib.parse import unquote
        self.assertEqual(unquote(pointer.read_text().splitlines()[1]),uri)

    def test_readdir_fallback_finds_older_matching_translation(self):
        self.scan();self.lua.globals().translate_subtitles()
        original=self.latest_job();self.status(original,'done',output='/tmp/movie.ru.srt')
        for pointer in self.root.glob('video-*.txt'):pointer.unlink()
        self.scan()
        self.lua.globals().read_directory=lambda path:self.lua.table_from([p.name for p in Path(path).iterdir()])
        self.lua.execute('vlc.io.readdir=function(path) return read_directory(path) end')
        self.lua.globals().deactivate();self.lua.globals().widgets=self.lua.table()
        self.lua.globals().activate()
        self.assertEqual(self.lua.globals().loads[1][1],'/tmp/movie.ru.srt')

    def test_reopen_recovers_completed_scan_without_starting_another(self):
        self.scan()
        self.lua.globals().deactivate()
        self.lua.globals().widgets = self.lua.table()
        self.lua.globals().activate()
        self.assertEqual(len(self.widget('dropdown')['values']), 3)
        self.assertEqual(len(self.lua.globals().commands), 1)

    def test_quick_scan_populates_without_manual_refresh(self):
        def result_ready():
            job = self.latest_job()
            (job/'choices.tsv').write_text('track\t2\tEnglish')
            self.status(job, 'discovered')
        self.lua.globals().result_ready = result_ready
        self.lua.execute('os.execute = function(command) table.insert(commands,command); result_ready(); return 0 end')
        self.lua.globals().scan_sources()
        self.assertEqual(self.widget('dropdown')['values'][1], 'English')

    def test_unicode_status_is_latin1_safe_html(self):
        self.scan()
        self.status(self.latest_job(), 'error')
        p = self.latest_job() / 'status.txt'
        p.write_text(p.read_text().replace('Test%20status', quote('Не готово — ошибка', safe='')))
        self.lua.globals().check_progress()
        status = next(w['text'] for w in self.lua.globals().widgets.values() if w['text'].startswith('Could not finish:'))
        self.assertTrue(status.isascii())
        import html
        self.assertIn('Не готово — ошибка', html.unescape(status))


if __name__ == '__main__':
    unittest.main()

    def test_cancel_abandoned_ocr_resets_primary_button(self):
        from codex_subs.cli import cancel_job
        self.scan()
        self.lua.globals().translate_subtitles()
        job=self.latest_job()
        self.status(job,'ocr')
        self.lua.globals().check_progress()
        self.widget('Translation is running')
        self.lua.globals().cancel_helper=lambda: cancel_job(job)
        self.lua.execute("os.execute=function(command) table.insert(commands,command); if string.find(command,'cancel-job',1,true) then cancel_helper() end; return 0 end")
        self.lua.globals().cancel_job()
        self.widget('Create Russian subtitles')
        self.assertEqual((job/'status.txt').read_text().splitlines()[1],'cancelled')
        self.assertTrue(any('Cancelled.' in w['text'] for w in self.lua.globals().widgets.values()))

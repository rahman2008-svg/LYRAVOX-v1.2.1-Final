"""The storage fix. REAL: the downloader, real HTTP streaming from a local mock "Hugging Face hub", real disk writes, a real size-limited tmpfs (ENOSPC),
real threads. MOCK: (1) the hub itself (local server, same URL scheme as huggingface.co), (2) in the last class only, the faster_whisper module, to
prove the app asks it for a LOCAL folder and builds exactly one model; no model weights or speech are involved anywhere in this file."""
import errno, json, os, shutil, subprocess, sys, tempfile, threading, time, types, unittest, importlib.machinery
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from helpers import *
from lyravox_ai import modelstore
from lyravox_ai.config import Config
from lyravox_ai.errors import ApiError, ModelError

REPO = 'Systran/faster-whisper-tiny'
MB = 1024 * 1024


class Hub:
    """Minimal imitation of huggingface.co: /api/models/<repo>?blobs=true, /<repo>/resolve/main/<file> -> 302 -> /cdn/<file>."""
    def __init__(self, model_mb=3.0, lie_sizes=False):
        self.files = {'config.json': json.dumps({'a': 1}).encode(), 'preprocessor_config.json': b'{"b":2}', 'tokenizer.json': b'{"t":[]}', 'vocabulary.txt': b'tok\n' * 50,
                      'model.bin': os.urandom(int(model_mb * MB)), 'README.md': b'readme', '.gitattributes': b'x'}
        self.hits, self.mode, self.lie = [], None, lie_sizes
        hub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *a): pass
            def do_HEAD(self): self.do_GET()
            def do_GET(self):
                hub.hits.append(self.path)
                if self.path.startswith(f'/api/models/{REPO}'):
                    sib = [{'rfilename': n, 'size': (100_000 if (hub.lie and n == 'model.bin') else len(b))} for n, b in hub.files.items()]
                    body = json.dumps({'siblings': sib}).encode(); self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
                if self.path.startswith(f'/{REPO}/resolve/main/'):
                    self.send_response(302); self.send_header('Location', '/cdn/' + self.path.rsplit('/', 1)[1]); self.send_header('Content-Length', '0'); self.end_headers(); return
                if self.path.startswith('/cdn/'):
                    name = self.path[5:]; data = hub.files.get(name)
                    if data is None: self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers(); return
                    self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
                    if self.command == 'HEAD': return
                    if hub.mode == 'truncate' and name == 'model.bin':
                        self.wfile.write(data[:len(data) // 2]); self.wfile.flush(); self.close_connection = True; return
                    if hub.mode == 'slow' and name == 'model.bin':
                        for i in range(0, len(data), 256 * 1024): self.wfile.write(data[i:i + 256 * 1024]); time.sleep(0.02)
                        return
                    self.wfile.write(data); return
                self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H); self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.srv.server_address[1]}'
        self._old = os.environ.get('LYRAVOX_HF_ENDPOINT'); os.environ['LYRAVOX_HF_ENDPOINT'] = self.url

    def count(self, frag): return sum(1 for h in self.hits if frag in h)

    def close(self):
        self.srv.shutdown(); self.srv.server_close()
        os.environ.pop('LYRAVOX_HF_ENDPOINT', None) if self._old is None else os.environ.__setitem__('LYRAVOX_HF_ENDPOINT', self._old)


def cfg_for(root, **kw):
    c = Config(); c.whisper_model = 'tiny'; c.model_cache = root; c.model_margin_mb = 1
    for k, v in kw.items(): setattr(c, k, v)
    return c


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(tmpdir(), 'cache'); self.hub = Hub()

    def tearDown(self):
        self.hub.close(); shutil.rmtree(os.path.dirname(self.root), ignore_errors=True)

    def partials(self):
        return [n for n in os.listdir(self.root) if n.endswith('.partial')] if os.path.isdir(self.root) else []


class DownloadOnce(StoreBase):
    def test_downloads_only_the_needed_files_into_one_folder(self):
        st = modelstore.ModelStore(cfg_for(self.root)); path = st.ensure()
        self.assertEqual(sorted(n for n in os.listdir(path) if not n.startswith('.')), ['config.json', 'model.bin', 'preprocessor_config.json', 'tokenizer.json', 'vocabulary.txt'])
        self.assertEqual(os.path.getsize(os.path.join(path, 'model.bin')), len(self.hub.files['model.bin']))
        self.assertEqual(self.hub.count('README'), 0); self.assertEqual(self.hub.count('gitattributes'), 0)
        self.assertEqual(os.listdir(self.root), ['model-tiny'])                       # ONE copy: no hub cache, no xet cache, no partial folders
        self.assertEqual(self.partials(), [])
        self.assertAlmostEqual(modelstore.dir_size_mb(self.root), 3.0, delta=0.1)     # disk use == model size, not 2x

    def test_second_use_and_a_new_store_object_cause_no_network_access(self):
        st = modelstore.ModelStore(cfg_for(self.root)); st.ensure()
        n = len(self.hub.hits); st.ensure(); modelstore.ModelStore(cfg_for(self.root)).ensure()
        self.assertEqual(len(self.hub.hits), n)                                        # complete on disk -> used as is
        self.assertEqual(st.downloads, 1)

    def test_concurrent_requests_share_one_download(self):
        self.hub.mode = 'slow'; st = modelstore.ModelStore(cfg_for(self.root)); paths, errs = [], []
        def go():
            try: paths.append(st.ensure())
            except Exception as e: errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, []); self.assertEqual(len(set(paths)), 1); self.assertEqual(self.hub.count('/cdn/model.bin'), 1)

    def test_prepare_env_keeps_hf_off_xet_and_inside_the_folder(self):
        old = {k: os.environ.pop(k, None) for k in ('HF_HUB_DISABLE_XET', 'HF_HOME')}
        try:
            modelstore.prepare_env(self.root)
            self.assertEqual(os.environ['HF_HUB_DISABLE_XET'], '1'); self.assertTrue(os.environ['HF_HOME'].startswith(self.root))
        finally:
            for k, v in old.items(): os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class FailureCleanup(StoreBase):
    def test_truncated_download_leaves_nothing_behind_and_a_retry_succeeds(self):
        self.hub.mode = 'truncate'; st = modelstore.ModelStore(cfg_for(self.root))
        with self.assertRaises(ModelError) as e: st.ensure()
        self.assertEqual(e.exception.stage, 'verify' if e.exception.reason == 'size_mismatch' else 'download')
        self.assertIn(e.exception.reason, ('size_mismatch', 'download_failed')); self.assertEqual(st.state, 'failed')
        self.assertEqual(self.partials(), []); self.assertAlmostEqual(modelstore.dir_size_mb(self.root), 0.0, delta=0.01)      # partial data deleted
        self.hub.mode = None; path = st.ensure()
        self.assertEqual(st.state, 'ready'); self.assertEqual(os.path.getsize(os.path.join(path, 'model.bin')), len(self.hub.files['model.bin']))

    def test_corrupt_json_is_rejected(self):
        self.hub.files['config.json'] = b'{not json'; st = modelstore.ModelStore(cfg_for(self.root))
        with self.assertRaises(ModelError) as e: st.ensure()
        self.assertEqual((e.exception.stage, e.exception.reason), ('verify', 'corrupt_file')); self.assertEqual(self.partials(), [])

    def test_leftovers_of_older_attempts_and_other_models_are_swept_first(self):
        os.makedirs(os.path.join(self.root, 'models--Systran--faster-whisper-tiny', 'blobs')); os.makedirs(os.path.join(self.root, 'hf', 'xet')); os.makedirs(os.path.join(self.root, 'model-other'))
        for p in ('models--Systran--faster-whisper-tiny/blobs/x.incomplete', 'hf/xet/chunk', 'model-other/model.bin'):
            with open(os.path.join(self.root, p), 'wb') as f: f.write(b'0' * 2 * MB)
        os.makedirs(os.path.join(self.root, 'model-tiny.partial'))
        st = modelstore.ModelStore(cfg_for(self.root)); st.ensure()
        self.assertEqual(os.listdir(self.root), ['model-tiny'])

    def test_a_model_folder_supplied_by_the_operator_is_used_without_any_download(self):
        own = os.path.join(tmpdir(), 'mymodel'); os.makedirs(own)
        with open(os.path.join(own, 'model.bin'), 'wb') as f: f.write(b'x' * 1000)
        st = modelstore.ModelStore(cfg_for(self.root, whisper_model=own)); self.assertEqual(st.ensure(), own)
        self.assertEqual(self.hub.hits, []); self.assertFalse(os.path.exists(self.root))

    def test_a_folder_without_model_bin_is_a_clear_error(self):
        own = os.path.join(tmpdir(), 'empty'); os.makedirs(own)
        with self.assertRaises(ModelError) as e: modelstore.ModelStore(cfg_for(self.root, whisper_model=own)).ensure()
        self.assertEqual((e.exception.stage, e.exception.reason), ('verify', 'model_files_missing'))


class FailFast(StoreBase):
    def test_model_larger_than_the_budget_is_refused_before_any_file_is_requested(self):
        st = modelstore.ModelStore(cfg_for(self.root, model_budget_mb=2))               # model is 3 MB
        with self.assertRaises(ModelError) as e: st.ensure()
        self.assertEqual((e.exception.stage, e.exception.reason), ('disk_check', 'model_too_large')); self.assertIn('Nothing was downloaded', e.exception.message)
        self.assertEqual(self.hub.count('/cdn/'), 0); self.assertEqual(self.hub.count('/resolve/'), 0)
        n = len(self.hub.hits)
        with self.assertRaises(ModelError): st.ensure()                                  # remembered: no further network traffic at all
        self.assertEqual(len(self.hub.hits), n)

    def test_not_enough_free_space_is_refused_before_downloading_MOCKED_DISK_USAGE(self):
        real = shutil.disk_usage
        shutil.disk_usage = lambda p: types.SimpleNamespace(total=100 * MB, used=99 * MB, free=1 * MB)
        try:
            with self.assertRaises(ModelError) as e: modelstore.ModelStore(cfg_for(self.root)).ensure()
        finally:
            shutil.disk_usage = real
        self.assertEqual((e.exception.stage, e.exception.reason), ('disk_check', 'insufficient_storage')); self.assertEqual(self.hub.count('/cdn/'), 0)

    def test_unreachable_hub_is_a_clear_resolve_error(self):
        os.environ['LYRAVOX_HF_ENDPOINT'] = 'http://127.0.0.1:9'
        with self.assertRaises(ModelError) as e: modelstore.ModelStore(cfg_for(self.root)).ensure()
        self.assertEqual((e.exception.stage, e.exception.reason), ('resolve', 'hub_unreachable'))

    def test_unknown_model_name(self):
        with self.assertRaises(ModelError) as e: modelstore.ModelStore(cfg_for(self.root, whisper_model='nonsense')).ensure()
        self.assertEqual(e.exception.reason, 'unknown_model')


class RealNoSpaceLeft(unittest.TestCase):
    """REAL ENOSPC: a 1 MB tmpfs. The mock hub under-reports model.bin (100 KB) so the pre-check passes and the disk fills DURING the download —
    the exact situation of the production error."""
    def test_disk_full_mid_download_is_caught_cleaned_and_reported(self):
        mnt = tempfile.mkdtemp(prefix='lvmnt-')
        r = subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=1m', 'tmpfs', mnt], capture_output=True)
        if r.returncode != 0:
            os.rmdir(mnt); self.skipTest('cannot mount a tmpfs here: ' + r.stderr.decode().strip())
        hub = Hub(model_mb=3.0, lie_sizes=True)
        try:
            st = modelstore.ModelStore(cfg_for(os.path.join(mnt, 'cache'), model_margin_mb=0))
            free_before = shutil.disk_usage(mnt).free
            with self.assertRaises(ModelError) as e: st.ensure()
            self.assertEqual((e.exception.stage, e.exception.reason), ('download', 'insufficient_storage'))
            self.assertIn('Ran out of storage', e.exception.message); self.assertEqual(st.state, 'failed'); self.assertEqual(st.failure['stage'], 'download')
            self.assertEqual([n for n in os.listdir(os.path.join(mnt, 'cache')) if n.endswith('.partial')], [])
            self.assertGreaterEqual(shutil.disk_usage(mnt).free, free_before - 4096)       # the partial data was really freed
            self.assertIn('disk', e.exception.diagnostics); self.assertLess(e.exception.diagnostics['disk']['total_mb'], 2)
        finally:
            hub.close(); subprocess.run(['umount', mnt]); os.rmdir(mnt)


class Diagnostics(StoreBase):
    def test_status_reports_disk_cache_model_and_failure_stage(self):
        st = modelstore.ModelStore(cfg_for(self.root, model_budget_mb=50))
        s0 = st.status(); self.assertEqual(s0['state'], 'idle'); self.assertIn('free_mb', s0['disk']); self.assertEqual(s0['cache']['budget_mb'], 50)
        st.ensure(); s1 = st.status()
        self.assertEqual((s1['state'], s1['downloads_this_process']), ('ready', 1)); self.assertAlmostEqual(s1['cache']['size_mb'], 3.0, delta=0.1)
        self.assertIn('model.bin', s1['files'])


def fake_faster_whisper(created):
    """MOCK of the faster_whisper module: records how the model is built and returns scripted segments."""
    mod = types.ModuleType('faster_whisper'); mod.__spec__ = importlib.machinery.ModuleSpec('faster_whisper', None)
    class Seg:
        def __init__(self, a, b, t): self.start, self.end, self.text, self.avg_logprob, self.no_speech_prob, self.compression_ratio, self.words = a, b, t, -0.2, 0.05, 1.0, []
    class Info:
        language, language_probability, all_language_probs = 'bn', 0.9, [('bn', 0.9), ('hi', 0.05)]
    class WhisperModel:
        def __init__(self, path, **kw):
            created.append((path, kw)); self.path = path
        def transcribe(self, audio, **kw):
            return iter([Seg(0.5, 2.5, 'আমার সোনার বাংলা')]), Info()
    mod.WhisperModel = WhisperModel
    return mod


class OneModelPerProcess(StoreBase):
    def setUp(self):
        super().setUp(); self.created = []
        self.saved = sys.modules.get('faster_whisper'); sys.modules['faster_whisper'] = fake_faster_whisper(self.created)
        from lyravox_ai import engines; engines._SHARED.clear()

    def tearDown(self):
        sys.modules.pop('faster_whisper', None) if self.saved is None else sys.modules.__setitem__('faster_whisper', self.saved)
        super().tearDown()

    def core(self, **kw):
        from lyravox_ai.core import Core
        from lyravox_ai.engines import FasterWhisperTranscriber
        cfg = cfg_for(self.root, rate_limit_per_min=0, **kw); return Core(cfg, FasterWhisperTranscriber(cfg))

    def test_model_is_built_once_from_a_local_folder_never_from_a_name(self):
        core = self.core(); errs, out = [], []
        def go():
            try: out.append(core.transcribe(wav_bytes(), {'language': 'bn'}, 'c'))
            except Exception as e: errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(6)]; [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, []); self.assertEqual(len(out), 6)
        self.assertEqual(len(self.created), 1, 'WhisperModel built more than once')
        path = self.created[0][0]; self.assertTrue(os.path.isdir(path) and os.path.isfile(os.path.join(path, 'model.bin')) and path != 'tiny')    # a folder: the library never downloads
        self.assertEqual(self.hub.count('/cdn/model.bin'), 1); self.assertEqual(os.listdir(self.root), ['model-tiny'])
        self.assertEqual(out[0]['segments'][0]['text'], 'আমার সোনার বাংলা'); self.assertEqual(core.health()['diagnostics']['state'], 'ready')

    def test_two_transcribers_in_one_process_still_share_one_model(self):
        c1, c2 = self.core(), self.core(); c1.transcribe(wav_bytes(), {'language': 'bn'}, 'a'); c2.transcribe(wav_bytes(), {'language': 'bn'}, 'b')
        self.assertEqual(len(self.created), 1)

    def test_download_failure_is_a_503_with_stage_and_diagnostics_and_nothing_is_faked(self):
        self.hub.mode = 'truncate'; core = self.core()
        with self.assertRaises(ApiError) as e: core.transcribe(wav_bytes(), {'language': 'en'}, 'c')
        err = e.exception.body()['error']
        self.assertEqual((e.exception.status, err['code']), (503, 'model_unavailable')); self.assertIn(err['stage'], ('download', 'verify')); self.assertIn('diagnostics', err)
        self.assertEqual(self.created, []); self.assertEqual(self.partials(), [])             # no model was built, no placeholder answer was produced
        self.hub.mode = None
        self.assertEqual(len(core.transcribe(wav_bytes(), {'language': 'en'}, 'c')['segments']), 1)      # the next request recovers

    def test_too_big_model_is_a_clear_503_not_a_disk_error(self):
        core = self.core(model_budget_mb=2)
        with self.assertRaises(ApiError) as e: core.transcribe(wav_bytes(), {'language': 'en'}, 'c')
        b = e.exception.body()['error']; self.assertEqual((e.exception.status, b['code'], b['stage'], b['reason']), (503, 'model_unavailable', 'disk_check', 'model_too_large'))

    def test_library_no_space_error_is_translated(self):
        core = self.core()
        class Boom(Exception): pass
        core.transcriber.model()
        def boom(*a, **k): raise RuntimeError('Task error: File reconstruction error: IO Error: No space left on device (os error 28)')
        core.transcriber._model.transcribe = boom
        with self.assertRaises(ApiError) as e: core.transcribe(wav_bytes(), {'language': 'en'}, 'c')
        b = e.exception.body()['error']; self.assertEqual((e.exception.status, b['code'], b['reason']), (503, 'model_unavailable', 'insufficient_storage'))

    def test_get_diagnostics_and_warmup_through_the_real_handler(self):
        import requests
        from lyravox_ai.core import set_core
        core = self.core(); set_core(core)
        srv, base = serve_handler(load_handler('transcribe'))
        try:
            d = requests.get(base + '/api/transcribe', timeout=10).json()
            self.assertEqual(d['diagnostics']['state'], 'idle'); self.assertEqual(self.created, [])
            w = requests.get(base + '/api/transcribe?warmup=1', timeout=30).json()
            self.assertEqual(w['diagnostics']['state'], 'ready'); self.assertTrue(w['diagnostics']['loaded']); self.assertEqual(len(self.created), 1)
            self.assertEqual(requests.put(base + '/api/transcribe', timeout=10).status_code, 501)
        finally:
            srv.shutdown(); srv.server_close()


if __name__ == '__main__':
    unittest.main()

"""API tests.
REAL: WAV parsing, the stateless core (validation, rate limit, silence skipping, language handling, offsets, timeouts, concurrency, honest
vocal-separation reporting), the real api/health.py and api/transcribe.py handler classes over real sockets, the dev server routing, and the real
'speech engine not installed' path (faster-whisper is not installed in the sandbox).
MOCK: the speech recogniser is the test-only placeholder (FakeTestTranscriber) wherever a successful transcription is needed."""
import json, os, socket, threading, time, unittest
import requests
from helpers import *
from lyravox_ai import web
from lyravox_ai.errors import ApiError, AudioError


class WavTests(unittest.TestCase):
    def test_roundtrip(self):
        pcm, dur = wav.parse(wav_bytes(3.0))
        self.assertAlmostEqual(dur, 3.0, places=2); self.assertEqual(pcm.dtype, np.float32); self.assertLessEqual(float(np.abs(pcm).max()), 1.0)

    def test_rejects_bad_formats(self):
        good = wav_bytes(1.0)
        cases = {'not riff': b'x' * 100, 'too short': good[:20], 'no data chunk': good[:36],
                 'stereo': good[:22] + b'\x02\x00' + good[24:], 'wrong rate': good[:24] + (44100).to_bytes(4, 'little') + good[28:],
                 '8 bit': good[:34] + b'\x08\x00' + good[36:], 'float tag': good[:20] + b'\x03\x00' + good[22:]}
        for name, body in cases.items():
            with self.subTest(name):
                with self.assertRaises(ApiError) as e: wav.parse(body)
                self.assertEqual(e.exception.status, 422)

    def test_empty_data_rejected(self):
        with self.assertRaises(ApiError) as e: wav.parse(wav.encode(np.zeros(0, dtype=np.float32)))
        self.assertEqual(e.exception.code, 'empty_audio')

    def test_extra_chunks_before_data_are_skipped(self):
        b = wav_bytes(1.0); fmt_end = 36
        extra = b'LIST' + (6).to_bytes(4, 'little') + b'abcdef'
        pcm, dur = wav.parse(b[:fmt_end] + extra + b[fmt_end:])
        self.assertAlmostEqual(dur, 1.0, places=2)


class CoreTests(unittest.TestCase):
    def test_success_shape_offset_and_times(self):
        core, cfg, eng = make_core()
        r = core.transcribe(wav_bytes(10.0, 3.0, 2.0), {'language': 'bn', 'offset': '100'}, 'c')
        self.assertEqual((r['language'], r['language_detected'], r['engine']), ('bn', False, 'fake-test-engine'))
        s = r['segments']
        self.assertEqual(len(s), 2)
        self.assertAlmostEqual(s[0]['start'], 100.0, delta=0.15); self.assertAlmostEqual(s[0]['end'], 103.0, delta=0.15)       # tone 0-3 s, offset +100
        self.assertAlmostEqual(s[1]['start'], 105.0, delta=0.15); self.assertAlmostEqual(s[1]['end'], 108.0, delta=0.15)       # tone 5-8 s
        self.assertTrue(all(w['start'] >= 100 for w in s[0]['words']))
        self.assertEqual(r['vocal_separation'], {'requested': False, 'used': False, 'error': None})

    def test_auto_language_is_detected_and_reported(self):
        core, *_ = make_core()
        r = core.transcribe(wav_bytes(), {'language': 'auto'}, 'c')
        self.assertTrue(r['language_detected']); self.assertIn(r['language'], ('bn', 'en', 'hi')); self.assertIsNotNone(r['language_probability'])

    def test_silence_is_skipped_without_touching_the_engine(self):
        core, cfg, eng = make_core()
        r = core.transcribe(wav.encode(np.zeros(16000 * 5, dtype=np.float32)), {}, 'c')
        self.assertTrue(r['skipped_silence']); self.assertEqual(r['segments'], []); self.assertEqual(eng.calls, [])

    def test_quiet_noise_below_threshold_is_silence(self):
        core, cfg, eng = make_core()
        r = core.transcribe(wav.encode((np.random.RandomState(1).randn(16000 * 3) * 0.0005).astype(np.float32)), {}, 'c')
        self.assertTrue(r['skipped_silence']); self.assertEqual(eng.calls, [])

    def test_limits_and_params(self):
        core, cfg, _ = make_core(max_chunk_s=20)
        with self.assertRaises(ApiError) as e: core.transcribe(wav_bytes(30.0, 3, 2), {}, 'c')
        self.assertEqual((e.exception.status, e.exception.code), (413, 'chunk_too_long'))
        with self.assertRaises(ApiError) as e: core.transcribe(wav.encode(bursts(0.2, 0.2, 0.0)), {}, 'c')
        self.assertEqual(e.exception.code, 'audio_too_short')
        for bad in ({'language': 'xx'}, {'offset': '-1'}, {'offset': 'abc'}, {'vocal_separation': 'maybe'}, {'word_timestamps': '2'}):
            with self.subTest(bad), self.assertRaises(ApiError) as e: core.transcribe(wav_bytes(), bad, 'c')
            self.assertEqual(e.exception.status, 422)

    def test_rate_limit_with_retry_after(self):
        core, *_ = make_core(rate_limit_per_min=3)
        for _ in range(3): core.transcribe(wav_bytes(2.0, 1, 1), {}, 'same')
        with self.assertRaises(ApiError) as e: core.transcribe(wav_bytes(2.0, 1, 1), {}, 'same')
        self.assertEqual((e.exception.status, e.exception.code), (429, 'rate_limited')); self.assertIn('Retry-After', e.exception.headers)
        core.transcribe(wav_bytes(2.0, 1, 1), {}, 'other')              # another client is unaffected

    def test_engine_failure_is_structured(self):
        core, *_ = make_core(fail_first=1)
        with self.assertRaises(ApiError) as e: core.transcribe(wav_bytes(), {}, 'c')
        self.assertEqual((e.exception.status, e.exception.code), (500, 'transcription_failed')); self.assertIn('simulated engine failure', e.exception.message)
        self.assertEqual(len(core.transcribe(wav_bytes(), {}, 'c')['segments']), 2)      # and the next request works

    def test_timeout_is_504(self):
        core, *_ = make_core(delay=0.6, request_timeout_s=0.5)
        with self.assertRaises(ApiError) as e: core.transcribe(wav_bytes(20.0, 2, 2), {}, 'c')
        self.assertEqual((e.exception.status, e.exception.code), (504, 'timeout'))

    def test_inference_is_serialised_under_concurrency(self):
        core, cfg, eng = make_core(rate_limit_per_min=0)
        live, peak, lock = [0], [0], threading.Lock()
        orig = eng.transcribe
        def tracked(*a, **k):
            with lock: live[0] += 1; peak[0] = max(peak[0], live[0])
            try:
                time.sleep(0.15); yield from orig(*a, **k)
            finally:
                with lock: live[0] -= 1
        eng.transcribe = tracked
        out, errs = [], []
        def go():
            try: out.append(core.transcribe(wav_bytes(), {'language': 'en'}, 'c'))
            except Exception as e: errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, []); self.assertEqual(len(out), 6); self.assertEqual(peak[0], 1)

    def test_rate_limit_table_does_not_grow_without_bound(self):
        core, *_ = make_core(rate_limit_per_min=5)
        for i in range(6000): core._rate(f'c{i}')
        self.assertLessEqual(len(core._hits), 5000)


class VocalSeparationHonesty(unittest.TestCase):
    class Sep:
        def __init__(self, mode): self.mode = mode
        def available(self): return self.mode != 'missing'
        def reason(self): return None if self.mode != 'missing' else 'Vocal separation is not installed on this server (pip install demucs). The original audio is used.'
        def separate(self, pcm):
            if self.mode == 'fail': raise AudioError('Vocal separation failed: simulated')
            return pcm * 0.5                                       # MOCK separator

    def run_with(self, mode):
        core, *_ = make_core(); core.separator = self.Sep(mode)
        return core.transcribe(wav_bytes(), {'vocal_separation': '1', 'language': 'en'}, 'c')

    def test_requested_but_unavailable_is_reported_not_faked(self):
        r = self.run_with('missing')
        self.assertEqual(r['vocal_separation']['requested'], True); self.assertFalse(r['vocal_separation']['used']); self.assertIn('not installed', r['vocal_separation']['error'])
        self.assertEqual(len(r['segments']), 2)                    # transcription still happens on the original audio

    def test_failure_falls_back_with_warning(self):
        r = self.run_with('fail')
        self.assertFalse(r['vocal_separation']['used']); self.assertTrue(any('original audio was used' in w for w in r['warnings'])); self.assertEqual(len(r['segments']), 2)

    def test_success_path_MOCK_SEPARATOR(self):
        r = self.run_with('ok'); self.assertTrue(r['vocal_separation']['used']); self.assertIsNone(r['vocal_separation']['error'])

    def test_real_demucs_class_never_available_on_vercel(self):
        from lyravox_ai.engines import DemucsSeparator
        old = os.environ.get('VERCEL'); os.environ['VERCEL'] = '1'
        try:
            d = DemucsSeparator(Config()); self.assertFalse(d.available()); self.assertIn('Vercel', d.reason())
            core = Core(Config(), FakeTestTranscriber(Config()), d)
            h = core.health(); self.assertEqual(h['runtime'], 'vercel'); self.assertFalse(h['vocal_separation']['available'])
            r = core.transcribe(wav_bytes(), {'vocal_separation': '1', 'language': 'en'}, 'c')
            self.assertFalse(r['vocal_separation']['used']); self.assertIn('Vercel', r['vocal_separation']['error'])
        finally:
            os.environ.pop('VERCEL', None) if old is None else os.environ.__setitem__('VERCEL', old)


class RealEngineMissing(unittest.TestCase):
    """REAL: this sandbox has no faster-whisper, so the genuine 'engine not installed' behaviour is what is under test."""
    def setUp(self):
        from lyravox_ai.engines import FasterWhisperTranscriber
        if FasterWhisperTranscriber.installed(): self.skipTest('faster-whisper IS installed here; this test covers the not-installed path')
        self.core = Core(Config())

    def test_health_says_degraded_with_reason(self):
        h = self.core.health(); self.assertEqual((h['status'], h['engine_available']), ('degraded', False)); self.assertIn('not installed', h['engine_reason'])

    def test_transcribe_returns_503_not_fake_lyrics(self):
        with self.assertRaises(ApiError) as e: self.core.transcribe(wav_bytes(), {'language': 'en'}, 'c')
        self.assertEqual((e.exception.status, e.exception.code), (503, 'engine_unavailable'))

    def test_silent_piece_still_answers_200(self):
        self.assertTrue(self.core.transcribe(wav.encode(np.zeros(16000 * 2, dtype=np.float32)), {}, 'c')['skipped_silence'])


class HttpHandlerTests(unittest.TestCase):
    """The exact api/transcribe.py and api/health.py handler classes, over real sockets."""
    @classmethod
    def setUpClass(cls):
        cls.core, cls.cfg, cls.eng = make_core(max_request_bytes=1_000_000, rate_limit_per_min=0)
        set_core(cls.core)
        cls.hs, cls.hbase = serve_handler(load_handler('health'))
        cls.ts, cls.tbase = serve_handler(load_handler('transcribe'))
        cls.body = wav_bytes(10.0, 3.0, 2.0)

    @classmethod
    def tearDownClass(cls):
        cls.hs.shutdown(); cls.ts.shutdown(); cls.hs.server_close(); cls.ts.server_close()

    def post(self, body=None, ctype='audio/wav', params=None, headers=None):
        h = {'Content-Type': ctype}; h.update(headers or {})
        return requests.post(self.tbase + '/api/transcribe', params=params or {'language': 'bn'}, data=self.body if body is None else body, headers=h, timeout=30)

    def test_health_endpoint(self):
        r = requests.get(self.hbase + '/api/health', timeout=10); j = r.json()
        self.assertEqual(r.status_code, 200); self.assertEqual(j['engine'], 'fake-test-engine'); self.assertEqual(j['languages'], ['bn', 'en', 'hi'])
        self.assertEqual(r.headers['Cache-Control'], 'no-store'); self.assertIn('limits', j); self.assertEqual(j['audio_format']['sample_rate'], 16000)
        self.assertEqual(requests.head(self.hbase + '/api/health', timeout=10).status_code, 200)

    def test_transcribe_ok(self):
        r = self.post(params={'language': 'bn', 'offset': '12.5'}); j = r.json()
        self.assertEqual(r.status_code, 200); self.assertEqual(len(j['segments']), 2); self.assertAlmostEqual(j['segments'][0]['start'], 12.5, delta=0.15)
        self.assertNotIn('Access-Control-Allow-Origin', r.headers)                                # same origin only: no CORS

    def test_errors_are_structured_json(self):
        for r, status, code in ((self.post(ctype='application/json'), 415, 'unsupported_type'), (self.post(body=b'not a wav file at all' * 10), 422, 'invalid_audio'),
                                (self.post(params={'language': 'zz'}), 422, 'unsupported_language'),
                                (self.post(body=b'\0' * 2_000_000), 413, 'payload_too_large'),
                                (requests.post(self.hbase + '/api/health', timeout=10), 405, 'method_not_allowed')):
            with self.subTest(code):
                self.assertEqual(r.status_code, status); self.assertEqual(r.json()['error']['code'], code)

    def test_unsupported_method_is_405_and_get_is_diagnostics(self):
        self.assertEqual(requests.head(self.tbase + '/api/transcribe', timeout=10).status_code, 405)
        g = requests.get(self.tbase + '/api/transcribe', timeout=10); self.assertEqual(g.status_code, 200); self.assertIn('diagnostics', g.json())

    def test_missing_content_length_is_411(self):
        s = socket.create_connection(('127.0.0.1', self.ts.server_address[1]), timeout=5)
        s.sendall(b'POST /api/transcribe HTTP/1.1\r\nHost: x\r\nContent-Type: audio/wav\r\nConnection: close\r\n\r\n')
        chunks = []
        while True:
            part = s.recv(4096)
            if not part:
                break
            chunks.append(part)
        s.close()
        data = b''.join(chunks).decode('utf-8', errors='replace')
        self.assertIn(' 411 ', data.split('\r\n')[0]); self.assertIn('length_required', data)

    def test_cross_site_requests_are_refused(self):
        r = self.post(headers={'Sec-Fetch-Site': 'cross-site'}); self.assertEqual((r.status_code, r.json()['error']['code']), (403, 'cross_site_blocked'))
        r = self.post(headers={'Origin': 'https://evil.example'}); self.assertEqual(r.status_code, 403)
        r = self.post(headers={'Sec-Fetch-Site': 'same-origin', 'Origin': self.tbase}); self.assertEqual(r.status_code, 200)

    def test_client_ip_from_forwarded_header_is_used_for_rate_limit(self):
        core, *_ = make_core(rate_limit_per_min=2); old = self.core
        set_core(core)
        try:
            codes = [self.post(headers={'X-Forwarded-For': '9.9.9.9'}).status_code for _ in range(3)]
            other = self.post(headers={'X-Forwarded-For': '8.8.8.8'}).status_code
            self.assertEqual(codes, [200, 200, 429]); self.assertEqual(other, 200)
        finally:
            set_core(old)

    def test_engine_error_surfaces_as_500_json(self):
        core, *_ = make_core(fail_first=1, rate_limit_per_min=0); old = self.core
        set_core(core)
        try:
            r = self.post(); self.assertEqual((r.status_code, r.json()['error']['code']), (500, 'transcription_failed'))
        finally:
            set_core(old)


class DevServerRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lyravox_ai import devserver
        pub = os.path.join(tmpdir(), 'public'); os.makedirs(os.path.join(pub, 'studio'))
        for p, c in (('index.html', 'HOME'), ('studio/index.html', 'STUDIO'), ('404.html', 'NOT FOUND PAGE')):
            with open(os.path.join(pub, p), 'w') as f: f.write(c)
        set_core(make_core(rate_limit_per_min=0)[0])
        cls.srv = devserver.serve('127.0.0.1', 0, pub); threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.srv.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def test_static_pages_and_api_share_one_origin(self):
        self.assertEqual(requests.get(self.base + '/', timeout=5).text, 'HOME'); self.assertEqual(requests.get(self.base + '/studio/', timeout=5).text, 'STUDIO')
        self.assertEqual(requests.get(self.base + '/api/health', timeout=5).json()['engine'], 'fake-test-engine')
        r = requests.post(self.base + '/api/transcribe?language=en', data=wav_bytes(), headers={'Content-Type': 'audio/wav'}, timeout=10); self.assertEqual(r.status_code, 200)

    def test_custom_404_and_api_404(self):
        r = requests.get(self.base + '/nope/x', timeout=5); self.assertEqual((r.status_code, r.text), (404, 'NOT FOUND PAGE'))
        r = requests.get(self.base + '/api/unknown', timeout=5); self.assertEqual((r.status_code, r.json()['error']['code']), (404, 'not_found'))


if __name__ == '__main__':
    unittest.main()

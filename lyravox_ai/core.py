"""The whole AI service in one framework-free module: validation, rate limiting, optional vocal separation, language detection, transcription and
line building for ONE chunk of audio. It is stateless (nothing is stored between requests, nothing is written to disk), which is what serverless
needs: the browser cuts the song into small chunks, sends them one by one and assembles the result.

Used by api/health.py and api/transcribe.py (Vercel) and by the local dev server — the same code in both."""
import math, os, threading, time
from collections import deque
from . import __version__, lines as L, wav
from .config import Config, on_vercel
from .engines import DemucsSeparator, FakeTestTranscriber, FasterWhisperTranscriber
from .errors import ApiError, AudioError, JobTimeout, ModelError


class Core:
    def __init__(self, cfg=None, transcriber=None, separator=None):
        self.cfg = cfg or Config.from_env()
        self.transcriber = transcriber or FasterWhisperTranscriber(self.cfg)
        self.separator = separator or DemucsSeparator(self.cfg)
        self.infer_lock = threading.Lock()                # one inference at a time per instance (CPU bound; avoids memory spikes)
        self._hits = {}
        self._hits_lock = threading.Lock()

    # -- health ---------------------------------------------------------------------------------------------------------
    def health(self):
        c = self.cfg
        ok = self.transcriber.available()
        sep_reason = self.separator.reason()
        return {
            'status': 'ok' if ok else 'degraded', 'version': __version__, 'engine': self.transcriber.name, 'engine_available': ok,
            'engine_reason': self.transcriber.reason(), 'model': getattr(self.transcriber, 'model_name', ''), 'model_loaded': bool(getattr(self.transcriber, 'loaded', False)),
            'languages': c.languages, 'runtime': 'vercel' if on_vercel() else 'local',
            'vocal_separation': {'available': sep_reason is None, 'reason': sep_reason},
            'limits': {'max_request_bytes': c.max_request_bytes, 'chunk_seconds': c.chunk_s, 'max_chunk_seconds': c.max_chunk_s, 'max_duration_s': c.max_duration_s,
                       'request_timeout_s': c.request_timeout_s, 'rate_limit_per_min': c.rate_limit_per_min},
            'audio_format': {'container': 'wav', 'codec': 'pcm_s16le', 'sample_rate': wav.RATE, 'channels': wav.CHANNELS},
            'diagnostics': self.diagnostics(),
        }

    def diagnostics(self):
        """Disk, cache and model status of THIS process (on Vercel each function has its own /tmp: ask /api/transcribe for the transcribing one)."""
        fn = getattr(self.transcriber, 'diagnostics', None)
        d = fn() if fn else {'name': getattr(self.transcriber, 'model_name', ''), 'state': 'ready', 'loaded': True}
        d['runtime'] = 'vercel' if on_vercel() else 'local'
        return d

    def warmup(self):
        """Prepare (download + load) the model without transcribing anything. Real failures are reported, never hidden."""
        if not self.transcriber.available():
            raise ApiError(503, 'engine_unavailable', self.transcriber.reason() or 'The speech engine is not available.', extra={'diagnostics': self.diagnostics()})
        try:
            self.infer_lock.acquire()
            try:
                self.transcriber.model()
            finally:
                self.infer_lock.release()
        except ModelError as e:
            raise ApiError(503, 'model_unavailable', e.message, extra={'stage': e.stage, 'reason': e.reason, 'diagnostics': self.diagnostics()})
        return self.diagnostics()

    # -- rate limit (best effort: per instance, in memory) ------------------------------------------------------------------
    def _rate(self, client):
        lim = self.cfg.rate_limit_per_min
        if lim <= 0:
            return
        now = time.monotonic()
        with self._hits_lock:
            q = self._hits.setdefault(client, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= lim:
                wait = max(1, int(60 - (now - q[0])) + 1)
                raise ApiError(429, 'rate_limited', f'Too many requests. Try again in {wait} s.', {'Retry-After': str(wait)})
            q.append(now)
            if len(self._hits) > 5000:                    # memory stays bounded even under a flood of distinct client addresses
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
                    self._hits.pop(k, None)
                if len(self._hits) > 5000:                # still too many fresh ones: forget the least recently active
                    for k, _ in sorted(self._hits.items(), key=lambda kv: kv[1][-1])[:len(self._hits) - 4000]:
                        self._hits.pop(k, None)

    # -- params -----------------------------------------------------------------------------------------------------------
    def parse_params(self, q):
        c = self.cfg
        lang = (q.get('language') or 'auto').strip().lower()
        if lang != 'auto' and lang not in c.languages:
            raise ApiError(422, 'unsupported_language', f"Language '{lang}' is not supported. Use 'auto' or one of: {', '.join(c.languages)}.")

        def b(name, default):
            v = q.get(name)
            if v is None or v == '': return default
            if str(v).lower() in ('1', 'true', 'yes', 'on'): return True
            if str(v).lower() in ('0', 'false', 'no', 'off'): return False
            raise ApiError(422, 'invalid_parameter', f"'{name}' must be 0 or 1.")
        try:
            off = float(q.get('offset') or 0.0)
        except ValueError:
            raise ApiError(422, 'invalid_parameter', "'offset' must be a number of seconds.")
        if not (0.0 <= off <= 86400.0) or off != off:
            raise ApiError(422, 'invalid_parameter', "'offset' is out of range.")
        return {'language': lang, 'offset': off, 'word_timestamps': b('word_timestamps', True), 'vocal_separation': b('vocal_separation', False)}

    # -- the work ---------------------------------------------------------------------------------------------------------
    def transcribe(self, body, query, client='unknown'):
        import numpy as np
        c = self.cfg
        self._rate(client)
        p = self.parse_params(query)
        pcm, dur = wav.parse(body)
        if dur > c.max_chunk_s + 0.5:
            raise ApiError(413, 'chunk_too_long', f'This piece is {dur:.0f} s long; send pieces of at most {c.max_chunk_s} s (about {c.chunk_s} s recommended).')
        if dur < 0.3:
            raise ApiError(422, 'audio_too_short', 'This piece is shorter than 0.3 s.')
        warnings, sep = [], {'requested': p['vocal_separation'], 'used': False, 'error': None}
        base = {'language': None if p['language'] == 'auto' else p['language'], 'language_probability': None, 'language_probabilities': None, 'language_detected': False,
                'duration': round(dur, 2), 'offset': p['offset'], 'segments': [], 'vocal_separation': sep, 'warnings': warnings,
                'engine': self.transcriber.name, 'model': getattr(self.transcriber, 'model_name', ''), 'skipped_silence': False}
        rms = float(math.sqrt(float(np.mean(pcm.astype(np.float64) ** 2))))
        if rms < c.silence_rms:                          # silence / instrumental break: nothing to recognise, so the model is not even loaded
            base['skipped_silence'] = True
            return base
        if not self.transcriber.available():
            raise ApiError(503, 'engine_unavailable', self.transcriber.reason() or 'The speech engine is not available.')
        if p['vocal_separation']:
            why = self.separator.reason()
            if why is not None:
                sep['error'] = why; warnings.append(why)         # honest: requested, NOT performed
            else:
                try:
                    pcm = self.separator.separate(pcm); sep['used'] = True
                except AudioError as e:
                    sep['error'] = str(e); warnings.append(f'{e}. The original audio was used instead.')
        deadline = time.monotonic() + c.request_timeout_s
        if not self.infer_lock.acquire(timeout=max(1.0, c.request_timeout_s / 2)):
            raise ApiError(503, 'server_busy', 'The server is busy right now. Please try again in a moment.', {'Retry-After': '5'})
        try:
            try:
                eng = self.transcriber
                lang = p['language']
                if lang == 'auto':
                    lang, prob, probs = eng.detect_language(pcm, c.languages)
                    base.update(language=lang, language_probability=round(prob, 3), language_detected=True,
                                language_probabilities={k: round(v, 3) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:5]})
                raw = list(eng.transcribe(pcm, lang, p['word_timestamps'], deadline))
            except JobTimeout:
                raise ApiError(504, 'timeout', f'This piece took longer than {c.request_timeout_s:.0f} s. Use a smaller model or shorter pieces.')
            except ApiError:
                raise
            except ModelError as e:                            # the model could not be downloaded / verified / loaded: say where, never fake anything
                raise ApiError(503, 'model_unavailable', e.message, extra={'stage': e.stage, 'reason': e.reason, 'diagnostics': self.diagnostics()})
            except (OSError, ConnectionError) as e:            # model files could not be read
                raise ApiError(503, 'model_unavailable', f'The speech model could not be loaded: {type(e).__name__}: {e}'[:300], extra={'stage': 'load', 'diagnostics': self.diagnostics()})
            except MemoryError:
                raise ApiError(503, 'out_of_memory', 'The server ran out of memory. Use a smaller model.')
            except Exception as e:
                text = f'{type(e).__name__}: {e}'
                if 'No space left on device' in text or getattr(e, 'errno', None) == 28:        # storage ran out inside a library (e.g. a Hugging Face download)
                    raise ApiError(503, 'model_unavailable', f'The server ran out of temporary storage ({text[:200]}).', extra={'stage': 'transcribe', 'reason': 'insufficient_storage', 'diagnostics': self.diagnostics()})
                raise ApiError(500, 'transcription_failed', f'Transcription failed: {text}'[:300], extra={'stage': 'transcribe'})
        finally:
            self.infer_lock.release()
        lines = L.build_lines(raw)
        off = p['offset']
        for l in lines:
            l['start'] = round(l['start'] + off, 2); l['end'] = round(l['end'] + off, 2)
            for w in l['words']:
                w['start'] = round(w['start'] + off, 2); w['end'] = round(w['end'] + off, 2)
        base['segments'] = [{'id': l['id'], 'start': l['start'], 'end': l['end'], 'text': l['text'], 'confidence': l['confidence'], 'approx_timing': l['approx'], 'words': l['words']} for l in lines]
        return base


_core = None
_core_lock = threading.Lock()


def get_core():
    """The process-wide Core, built from the environment on first use."""
    global _core
    with _core_lock:
        if _core is None:
            cfg = Config.from_env()
            if os.environ.get('LYRAVOX_ENGINE') == 'fake':       # test-only; raises unless LYRAVOX_ALLOW_FAKE=1
                _core = Core(cfg, FakeTestTranscriber(cfg, delay=float(os.environ.get('LYRAVOX_FAKE_DELAY', '0') or 0),
                                                      fail_first=int(os.environ.get('LYRAVOX_FAKE_FAIL_FIRST', '0') or 0)))
            else:
                _core = Core(cfg)
        return _core


def set_core(core):
    global _core
    with _core_lock:
        _core = core

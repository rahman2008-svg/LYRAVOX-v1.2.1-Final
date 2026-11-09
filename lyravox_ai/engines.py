"""Speech-to-text and (optional) vocal-separation engines.

* FasterWhisperTranscriber — the real engine (faster-whisper / CTranslate2 running open-source Whisper models).
* DemucsSeparator          — vocal separation with Demucs. It CANNOT run on Vercel (see docs/VERCEL.md); it is available only on a host that
                             has the demucs package and ffmpeg, and everything else reports it as unavailable instead of pretending.
* FakeTestTranscriber      — TEST ONLY. Cuts the audio at silences and labels each stretch "TEST LINE n". It refuses to start unless
                             LYRAVOX_ALLOW_FAKE=1 and is never selected by default.
"""
import importlib.util, os, shutil, sys, tempfile, threading, time
from dataclasses import dataclass, field
from . import audio
from .config import on_vercel
from .errors import AudioError, JobTimeout, ModelError
from .modelstore import ModelStore, prepare_env

_SHARED = {}                      # one WhisperModel per (folder, device, compute) per process: a model is never loaded twice
_SHARED_LOCK = threading.Lock()


@dataclass
class Word:
    start: float
    end: float
    text: str
    probability: float = None


@dataclass
class RawSegment:
    start: float
    end: float
    text: str
    avg_logprob: float = None
    no_speech_prob: float = None
    compression_ratio: float = None
    words: list = field(default_factory=list)


class FasterWhisperTranscriber:
    name = 'faster-whisper'

    def __init__(self, cfg):
        self.cfg = cfg
        self.store = ModelStore(cfg)
        prepare_env(self.store.root)                       # before any Hugging Face library is imported: no Xet, everything inside our folder
        self._model = None
        self._lock = threading.Lock()
        self.load_ms = None

    @staticmethod
    def installed():
        return importlib.util.find_spec('faster_whisper') is not None

    def available(self):
        return self.installed()

    def reason(self):
        return None if self.installed() else 'The speech engine (faster-whisper) is not installed in this deployment.'

    @property
    def model_name(self):
        return self.cfg.whisper_model

    @property
    def loaded(self):
        return self._model is not None

    def diagnostics(self):
        d = self.store.status()
        d['loaded'] = self.loaded
        d['load_ms'] = self.load_ms
        return d

    def model(self):
        """The one WhisperModel of this process. Downloads the files once (ModelStore), loads them from a local folder, never twice."""
        with self._lock:
            if self._model is None:
                path = self.store.ensure()                 # ModelError(stage = resolve | disk_check | download | verify) on failure
                key = (path, self.cfg.whisper_device, self.cfg.whisper_compute)
                with _SHARED_LOCK:
                    if key not in _SHARED:
                        t0 = time.monotonic()
                        try:
                            from faster_whisper import WhisperModel          # lazy: /api/health stays instant. A folder path => no download by the library
                            _SHARED[key] = WhisperModel(path, device=self.cfg.whisper_device, compute_type=self.cfg.whisper_compute)
                        except Exception as e:
                            raise self.store._fail('load', 'model_load_failed', f'The model files in {path} could not be loaded: {type(e).__name__}: {e}')
                        self.load_ms = round((time.monotonic() - t0) * 1000)
                    self._model = _SHARED[key]
                self.store.state, self.store.stage = 'ready', None
            return self._model

    def detect_language(self, audio_f32, allowed):
        """(language, probability, {language: probability}) restricted to the allowed languages. Whisper's own pick can be outside the
        supported set (Bangla singing is often heard as Hindi or Urdu), so the best ALLOWED language wins."""
        _segs, info = self.model().transcribe(audio_f32, language=None, task='transcribe', beam_size=1, vad_filter=False, without_timestamps=True)
        probs = {}
        for item in (getattr(info, 'all_language_probs', None) or []):
            try: probs[item[0]] = float(item[1])
            except (TypeError, ValueError, IndexError): pass
        if not probs:
            probs = {info.language: float(info.language_probability)}
        cand = {k: v for k, v in probs.items() if k in allowed} or probs
        best = max(cand, key=cand.get)
        return best, cand[best], probs

    def transcribe(self, audio_f32, language, word_timestamps, deadline=None):
        segs, _info = self.model().transcribe(
            audio_f32, language=language, task='transcribe', beam_size=self.cfg.whisper_beam, word_timestamps=word_timestamps,
            vad_filter=True, vad_parameters={'min_silence_duration_ms': 500},
            condition_on_previous_text=False,              # stops a hallucinated phrase from repeating through a song
            no_speech_threshold=0.6, compression_ratio_threshold=2.4, log_prob_threshold=-1.0)
        for s in segs:
            if deadline is not None and time.monotonic() > deadline:
                raise JobTimeout()
            words = [Word(w.start, w.end, w.word, getattr(w, 'probability', None)) for w in (s.words or [])]
            yield RawSegment(s.start, s.end, s.text.strip(), s.avg_logprob, s.no_speech_prob, s.compression_ratio, words)


class DemucsSeparator:
    name = 'demucs'

    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def installed():
        return importlib.util.find_spec('demucs') is not None

    def available(self):
        return self.reason() is None

    def reason(self):
        if on_vercel():
            return ('Vocal separation (Demucs) cannot run on Vercel: it needs PyTorch, which does not fit comfortably in a function '
                    'bundle, and CPU separation is slower than real time, far beyond a function time limit. The original audio is used.')
        if not self.cfg.demucs_enabled:
            return 'Vocal separation is disabled on this server (DEMUCS_ENABLED=0). The original audio is used.'
        if not self.installed():
            return 'Vocal separation is not installed on this server (pip install demucs). The original audio is used.'
        if shutil.which('ffmpeg') is None:
            return 'Vocal separation needs ffmpeg, which is not installed on this server. The original audio is used.'
        return None

    def separate(self, audio_f32):
        """Return separated vocals as float32 16 kHz mono. Raises AudioError on any failure (the caller falls back to the original)."""
        from . import wav
        work = tempfile.mkdtemp(prefix='lv-demucs-')
        try:
            src = os.path.join(work, 'in.wav')
            with open(src, 'wb') as f:
                f.write(wav.encode(audio_f32))
            cmd = [sys.executable, '-m', 'demucs.separate', '-n', self.cfg.demucs_model, '--two-stems', 'vocals', '-d', 'cpu', '-o', os.path.join(work, 'out'), src]
            rc, _, err = audio.run_cmd(cmd, timeout=self.cfg.demucs_timeout_s)
            if rc != 0:
                raise AudioError('Vocal separation failed: ' + (err.strip().splitlines()[-1][:200] if err.strip() else 'unknown error'))
            import glob
            found = glob.glob(os.path.join(work, 'out', '*', '*', 'vocals.*'))
            if not found:
                raise AudioError('Vocal separation produced no vocal track')
            out16 = os.path.join(work, 'vocals16k.wav')
            audio.decode(found[0], out16)
            with open(out16, 'rb') as f:
                pcm, _ = wav.parse(f.read())
            return pcm
        except JobTimeout:
            raise AudioError('Vocal separation took too long')
        finally:
            shutil.rmtree(work, ignore_errors=True)


class FakeTestTranscriber:
    """TEST ONLY — never real transcription."""
    name = 'fake-test-engine'
    model_name = 'fake-test-engine'
    loaded = True

    def __init__(self, cfg, delay=0.0, fail_first=0):
        if os.environ.get('LYRAVOX_ALLOW_FAKE') != '1':
            raise RuntimeError('FakeTestTranscriber is for automated tests only (set LYRAVOX_ALLOW_FAKE=1)')
        self.cfg, self.delay, self.fail_first = cfg, delay, fail_first
        self.calls = []
        self._n = 0
        self._lock = threading.Lock()

    def available(self): return True
    def reason(self): return None

    def detect_language(self, audio_f32, allowed):
        self.calls.append(('detect', len(audio_f32)))
        lang = sorted(allowed)[0] if allowed else 'en'
        return lang, 0.9, {lang: 0.9}

    def transcribe(self, audio_f32, language, word_timestamps, deadline=None):
        import numpy as np
        with self._lock:
            self._n += 1
            n = self._n
        self.calls.append(('transcribe', len(audio_f32), language))
        if n <= self.fail_first:
            raise RuntimeError('simulated engine failure')
        rate, hop = 16000, 320                                  # 20 ms frames
        f = len(audio_f32) // hop
        rms = np.sqrt(np.mean(audio_f32[:f * hop].reshape(f, hop) ** 2, axis=1)) if f else np.array([])
        loud = rms > 0.01
        runs, i = [], 0
        while i < f:                                            # runs of sound separated by >= 0.4 s of quiet
            if loud[i]:
                j = i
                while j < f and (loud[j] or (j + 1 < f and loud[j:j + 20].any())):
                    j += 1
                runs.append((i * hop / rate, j * hop / rate)); i = j
            else:
                i += 1
        for k, (a, b) in enumerate(runs):
            if deadline is not None and time.monotonic() > deadline:
                raise JobTimeout()
            if self.delay:
                time.sleep(self.delay)
            label = f'TEST LINE {k + 1}'
            words = []
            if word_timestamps:
                toks = label.split(' ')
                words = [Word(a + (b - a) * q / len(toks), a + (b - a) * (q + 1) / len(toks), ' ' + t, 0.9) for q, t in enumerate(toks)]
            yield RawSegment(a, b, label, -0.2, 0.05, 1.0, words)

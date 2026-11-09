"""REAL speech-recognition tests with the actual faster-whisper model on real recordings.

These tests are SKIPPED unless all of the following are true (none of them were available in the sandbox where this repository was written,
which has no network, no faster-whisper and no speech recordings — so NO recognition result in this project has been verified there):

  1. `pip install faster-whisper` and the model can be loaded (WHISPER_MODEL, default 'base'; needs a download on first use)
  2. recordings exist:  tests/api/audio/bn.wav  en.wav  hi.wav   (any format ffmpeg reads; sung or spoken)
  3. a reference next to each recording:  bn.txt / en.txt / hi.txt  (the true words, one line per phrase)
  Optional timing reference:  bn.times.json  = [[start_s, end_s], ...] for each phrase.

Run:  LYRAVOX_REAL=1 python -m unittest discover -s tests -p test_real_transcription.py -t tests
Quality gate (adjustable with env): WER <= REAL_MAX_WER (default 0.6 — Whisper on singing is far from perfect), median start-time error <= REAL_MAX_START_ERR (default 1.0 s).
The numbers are printed so you can judge them yourself; scripts/eval_transcription.py gives the same report for any folder."""
import importlib.util, json, os, re, time, unittest
from helpers import *
from lyravox_ai import audio

AUDIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio')
HAVE = bool(importlib.util.find_spec('faster_whisper')) and os.environ.get('LYRAVOX_REAL') == '1'


def wer(ref, hyp):
    r, h = ref.split(), hyp.split()
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1): d[i][0] = i
    for j in range(len(h) + 1): d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    return d[len(r)][len(h)] / max(1, len(r))


def norm(t): return re.sub(r'[^\w\s]', '', t.lower()).strip()


@unittest.skipUnless(HAVE, 'faster-whisper not installed or LYRAVOX_REAL!=1: REAL transcription NOT tested in this environment')
class RealTranscription(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = Core(Config.from_env())

    def _transcribe_file(self, path, language):
        """Same thing the browser does: 16 kHz mono, cut at silences into pieces, one request per piece."""
        d = tmpdir(); wav16 = os.path.join(d, 'a.wav')
        audio.decode(path, wav16)
        dur = audio.probe(wav16)['duration']
        cuts = audio.plan_chunks(dur, audio.detect_silences(wav16), self.core.cfg.chunk_s)
        segs, lang = [], language
        for a, b in cuts:
            piece = os.path.join(d, 'p.wav'); audio.decode(wav16, piece, a, b)
            with open(piece, 'rb') as f: body = f.read()
            r = self.core.transcribe(body, {'language': lang, 'offset': str(a)}, 'real-test')
            if r.get('language_detected'): lang = r['language']
            segs += r['segments']
        return lang, segs

    def _run(self, lang, language):
        found = [os.path.join(AUDIO, f'{lang}.{e}') for e in ('wav', 'mp3', 'm4a', 'ogg', 'aac') if os.path.exists(os.path.join(AUDIO, f'{lang}.{e}'))]
        ref = os.path.join(AUDIO, f'{lang}.txt')
        if not found or not os.path.exists(ref):
            self.skipTest(f'no tests/api/audio/{lang}.* recording + {lang}.txt reference supplied')
        det, segs = self._transcribe_file(found[0], language)
        hyp = norm(' '.join(s['text'] for s in segs)); truth = norm(open(ref, encoding='utf-8').read())
        w = wer(truth, hyp)
        print(f'\n   [{lang}] language={det} WER={w:.2f} model={self.core.cfg.whisper_model} lines={len(segs)}')
        self.assertLessEqual(w, float(os.environ.get('REAL_MAX_WER', 0.6)))
        tj = os.path.join(AUDIO, f'{lang}.times.json')
        if os.path.exists(tj):
            ref_t = json.load(open(tj)); errs = sorted(abs(a['start'] - b[0]) for a, b in zip(segs, ref_t))
            med = errs[len(errs) // 2]; print(f'   [{lang}] median start-time error {med:.2f} s over {len(errs)} lines')
            self.assertLessEqual(med, float(os.environ.get('REAL_MAX_START_ERR', 1.0)))
        return det

    def test_bangla(self): self._run('bn', 'bn')
    def test_english(self): self._run('en', 'en')
    def test_hindi(self): self._run('hi', 'hi')

    def test_auto_detects_among_supported_languages(self):
        for lang in ('bn', 'en', 'hi'):
            if not any(os.path.exists(os.path.join(AUDIO, f'{lang}.{e}')) for e in ('wav', 'mp3', 'm4a', 'ogg', 'aac')): continue
            with self.subTest(lang=lang):
                self.assertEqual(self._run(lang, 'auto'), lang)


if __name__ == '__main__':
    unittest.main()

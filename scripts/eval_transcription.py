#!/usr/bin/env python3
"""Evaluate REAL transcription quality on your own recordings (needs `pip install -r requirements.txt` and ffmpeg for decoding the test files).

    python scripts/eval_transcription.py DIR [--language bn] [--model small]

DIR holds  name.<audio>  +  name.txt (reference words)  [+ name.times.json = [[start, end], ...] per phrase].
It does what the browser does: cuts each file into pieces at silences and sends them through the same core as /api/transcribe. Prints the detected
language, word error rate, character error rate and timestamp error, plus the transcript, so you can judge Bangla / English / Hindi on your material."""
import argparse, glob, json, os, re, sys, tempfile, time
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)
from lyravox_ai import audio
from lyravox_ai.config import Config
from lyravox_ai.core import Core


def edit(a, b):
    d = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        p, d[0] = d[0], i
        for j in range(1, len(b) + 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (a[i - 1] != b[j - 1]))
    return d[len(b)]


def norm(t): return re.sub(r'[^\w\s]', '', t.lower()).strip()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('dir'); ap.add_argument('--language', default='auto'); ap.add_argument('--model'); a = ap.parse_args()
    cfg = Config.from_env(); cfg.rate_limit_per_min = 0; cfg.request_timeout_s = 3600
    if a.model: cfg.whisper_model = a.model
    core = Core(cfg)
    for f in sorted(glob.glob(os.path.join(a.dir, '*'))):
        if not f.lower().endswith(('.wav', '.mp3', '.m4a', '.aac', '.ogg')): continue
        base = os.path.splitext(f)[0]; t0 = time.time(); work = tempfile.mkdtemp()
        w16 = os.path.join(work, 'a.wav'); audio.decode(f, w16)
        dur = audio.probe(w16)['duration']
        lang, segs = a.language, []
        for s, e in audio.plan_chunks(dur, audio.detect_silences(w16), cfg.chunk_s):
            piece = os.path.join(work, 'p.wav'); audio.decode(w16, piece, s, e)
            with open(piece, 'rb') as fh: body = fh.read()
            r = core.transcribe(body, {'language': lang, 'offset': str(s)}, 'eval')
            if r.get('language_detected'): lang = r['language']
            segs += r['segments']
        print(f'\n== {os.path.basename(f)}  {time.time() - t0:.0f}s  model={cfg.whisper_model}  language={lang}  lines={len(segs)}')
        hyp = norm(' '.join(s['text'] for s in segs))
        if os.path.exists(base + '.txt'):
            with open(base + '.txt', encoding='utf-8') as fh: ref = norm(fh.read())
            print(f'   WER={edit(ref.split(), hyp.split()) / max(1, len(ref.split())):.3f}  CER={edit(list(ref), list(hyp)) / max(1, len(ref)):.3f}')
        if os.path.exists(base + '.times.json'):
            with open(base + '.times.json') as fh: ref_t = json.load(fh)
            errs = sorted(abs(s['start'] - r[0]) for s, r in zip(segs, ref_t))
            if errs: print(f'   start error: median {errs[len(errs) // 2]:.2f}s  p90 {errs[int(len(errs) * .9)]:.2f}s  over {len(errs)} lines')
        for s in segs: print(f'   [{s["start"]:7.2f}-{s["end"]:7.2f}] ({s["confidence"]}) {s["text"]}')


if __name__ == '__main__':
    main()

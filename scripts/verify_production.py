#!/usr/bin/env python3
"""Verify the REAL AI pipeline of a running LYRAVOX (your Vercel deployment, or `python -m lyravox_ai.devserver`) with REAL speech recordings.

    python scripts/verify_production.py https://YOUR-APP.vercel.app --bn bangla.mp3 --en english.mp3 --hi hindi.mp3 [--ref-bn bangla.txt ...]

What it checks (exit code 0 only if ALL pass):
  1. /api/health: the speech engine is installed and it is the real faster-whisper engine (a placeholder/test engine FAILS this script);
  2. model download + load: GET /api/transcribe?warmup=1 -> diagnostics show state=ready, the files, disk free and cache size;
  3. for each language you supply: the recording is decoded locally (ffmpeg), cut at silences into ~30 s pieces, sent exactly like the browser does;
     the transcript must be non-empty, written in the right script (Bangla / Devanagari / Latin), timestamps must be ordered and inside the audio,
     and (if you pass a reference text) the word error rate is printed and must be <= --max-wer (default 0.8: singing is hard);
  4. auto language detection on each recording (reported; must return a supported language);
  5. no storage leak: the same recording is transcribed again and diagnostics must show the model was not downloaded again and the cache did not grow.
Needs: Python 3, ffmpeg + ffprobe on this machine (only to decode your test files)."""
import argparse, json, os, re, sys, tempfile, time, urllib.error, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from lyravox_ai import audio

SCRIPTS = {'bn': (re.compile(r'[\u0980-\u09ff]'), 'Bangla'), 'hi': (re.compile(r'[\u0900-\u097f]'), 'Devanagari'), 'en': (re.compile(r'[A-Za-z]'), 'Latin')}
results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))
    return ok


def call(url, method='GET', body=None, headers=None, timeout=330):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode('utf-8'))
        except ValueError: return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        return 0, {'error': {'code': 'network', 'message': str(e)}}


def wer(ref, hyp):
    r, h = ref.split(), hyp.split()
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        p, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (r[i - 1] != h[j - 1]))
    return d[len(h)] / max(1, len(r))


def norm(t): return re.sub(r'[^\w\s]', '', t.lower()).strip()


def transcribe_file(base, path, language, chunk_s=30):
    """Same protocol as the browser: 16 kHz mono WAV pieces, one request each, offsets on the song timeline."""
    work = tempfile.mkdtemp(); w16 = os.path.join(work, 'a.wav'); audio.decode(path, w16)
    dur = audio.probe(w16)['duration']
    segs, lang, errors = [], language, []
    for a, b in audio.plan_chunks(dur, audio.detect_silences(w16), chunk_s):
        piece = os.path.join(work, 'p.wav'); audio.decode(w16, piece, a, b)
        with open(piece, 'rb') as f: body = f.read()
        st, j = call(f'{base}/api/transcribe?language={lang}&offset={a:.3f}&word_timestamps=1', 'POST', body, {'Content-Type': 'audio/wav'})
        if st != 200:
            errors.append((st, j.get('error'))); break
        if j.get('language_detected') and j.get('language'): lang = j['language']
        segs += j['segments']
    return dur, lang, segs, errors


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('url'); ap.add_argument('--bn'); ap.add_argument('--en'); ap.add_argument('--hi')
    ap.add_argument('--ref-bn'); ap.add_argument('--ref-en'); ap.add_argument('--ref-hi'); ap.add_argument('--max-wer', type=float, default=0.8)
    a = ap.parse_args(); base = a.url.rstrip('/')
    print(f'\n== 1. engine  ({base})')
    st, h = call(base + '/api/health')
    if not check('/api/health answers', st == 200, f'HTTP {st}'): return finish()
    check('real faster-whisper engine is installed and selected', h.get('engine') == 'faster-whisper' and h.get('engine_available') is True, f"engine={h.get('engine')} available={h.get('engine_available')} reason={h.get('engine_reason')}")
    if h.get('engine') != 'faster-whisper': return finish()
    print(f"     model={h.get('model')} runtime={h.get('runtime')} vocal_separation={h.get('vocal_separation', {}).get('available')}")

    print('\n== 2. model download + load (first request on a cold instance can take a while)')
    t0 = time.time(); st, w = call(base + '/api/transcribe?warmup=1')
    d = (w or {}).get('diagnostics') or (w.get('error') or {}).get('diagnostics') or {}
    check('warm-up (download + load) succeeded', st == 200 and d.get('state') == 'ready' and d.get('loaded'), f"HTTP {st} {json.dumps(w.get('error') or {}, ensure_ascii=False)[:400]}")
    print(f"     {time.time() - t0:.0f}s  state={d.get('state')} stage={d.get('stage')} files={d.get('files')} load_ms={d.get('load_ms')}")
    print(f"     disk: {d.get('disk')}\n     cache: {d.get('cache')}")
    if st != 200: return finish()
    cache0, downloads0 = (d.get('cache') or {}).get('size_mb'), d.get('downloads_this_process')

    print('\n== 3. real transcription')
    todo = [(l, getattr(a, l), getattr(a, 'ref_' + l)) for l in ('bn', 'en', 'hi') if getattr(a, l)]
    if not todo: check('at least one recording supplied (--bn / --en / --hi)', False)
    first = None
    for lang, path, ref in todo:
        print(f'  -- {lang}: {path}')
        t0 = time.time(); dur, used, segs, errors = transcribe_file(base, path, lang)
        first = first or (lang, path)
        if not check(f'[{lang}] every piece answered HTTP 200', not errors, json.dumps(errors, ensure_ascii=False)[:500]): continue
        text = ' '.join(s['text'] for s in segs)
        check(f'[{lang}] transcript is not empty', len(text.strip()) > 0, f'{len(segs)} lines in {time.time() - t0:.0f}s')
        rx, nm = SCRIPTS[lang]; letters = [c for c in text if c.isalpha()]
        check(f'[{lang}] text is written in {nm} script', bool(letters) and sum(1 for c in letters if rx.match(c)) / len(letters) > 0.6, text[:80])
        starts = [s['start'] for s in segs]
        check(f'[{lang}] timestamps are ordered, non-negative and inside the audio ({dur:.1f}s)', starts == sorted(starts) and all(0 <= s['start'] < s['end'] <= dur + 1.0 for s in segs), starts[:6])
        if ref:
            with open(ref, encoding='utf-8') as f: wv = wer(norm(f.read()), norm(text))
            check(f'[{lang}] word error rate {wv:.2f} <= {a.max_wer}', wv <= a.max_wer)
        for s in segs[:8]: print(f"       [{s['start']:7.2f}-{s['end']:7.2f}] ({s['confidence']}) {s['text']}")
        # 4. auto detection on the same recording
        _, det, _, e2 = transcribe_file(base, path, 'auto')
        check(f'[{lang}] auto-detect returns a supported language', not e2 and det in h.get('languages', []), f'detected={det} (expected {lang})')

    if first:
        print('\n== 5. no storage leak (repeat the first recording)')
        transcribe_file(base, first[1], first[0])
        st, g = call(base + '/api/transcribe'); d2 = (g or {}).get('diagnostics') or {}
        print(f"     disk: {d2.get('disk')}\n     cache: {d2.get('cache')}")
        check('model was not downloaded again', d2.get('downloads_this_process') in (downloads0, 0) or d2.get('downloads_this_process') == downloads0, f"{downloads0} -> {d2.get('downloads_this_process')}")
        check('cache did not grow', cache0 is not None and (d2.get('cache') or {}).get('size_mb', 1e9) <= cache0 + 1.0, f"{cache0} MB -> {(d2.get('cache') or {}).get('size_mb')} MB")
    return finish()


def finish():
    ok = all(results) and results
    print(f"\n{sum(results)}/{len(results)} checks passed -> {'ALL PASSED' if ok else 'FAILED'}")
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()

# LYRAVOX — Turn Every Lyric Into a Visual Story.   _v1.2.1 — one project, one Vercel deployment_

Independent online lyric motion video studio. Write lyrics, add a song, get beat-timed animated typography and export
MP4 / PNG sequence / transparent PNG — all in the browser. No account, no upload, no API keys.

- Developer: **Prince AR Abdur Rahman** · Publisher: **NexVora Lab's Ofc** · Bangladesh · v1.0.0
- Portfolio: https://prince-ar-abdur-rahman.vercel.app
- Based on **JIZURA** by hakoniwa (MIT) — https://github.com/852wa/JIZURA — see `LICENSE`, `THIRD_PARTY_NOTICES.md`.

## Test status (v1.2.1) — read this first
* **The real Whisper test has NOT been run.** No real model download, no real model loading and no real Bangla / English / Hindi transcription (accuracy, timestamps, language detection) was executed: the development environment had no network, no `faster-whisper`, no model and no recordings. The production "No space left on device" error is therefore **not proven fixed**; the cause was identified and a fix implemented and tested against a mock hub, a real size-limited tmpfs and a mocked `faster_whisper` module.
* To run the real test on your deployment: `python scripts/verify_production.py https://YOUR-APP.vercel.app --bn bangla.mp3 --en english.mp3 --hi hindi.mp3`.
* Details of what was and was not run: [docs/TEST_REPORT.md](docs/TEST_REPORT.md).

## What is in the box
**One repository = one Vercel project = one URL.** Static site and AI API are deployed together.

| URL | What |
|---|---|
| `/` | Landing page (EN / বাংলা) |
| `/studio/` | Dashboard: projects, import, audio library, lyrics studio, export history (IndexedDB, this browser only) |
| `/editor/`, `/editor/bn/` | The editor (English, বাংলা). Also `/editor/ja/ zh-hant/ zh-hans/ ko/ id/ vi/` |
| `/about/ /developer/ /credits/ /privacy/ /terms/ /contact/` | Pages, `404.html`, `sitemap.xml`, `robots.txt`, `manifest.webmanifest` |
| `GET /api/health`, `POST /api/transcribe` | AI Generate Lyrics API (Python Functions, same origin) — see [docs/API.md](docs/API.md) |

The editor is the original JIZURA engine (860 motion components, beat detection, tap-sync, timeline, MP4 / PNG export, LRC, project save/load, aspect ratios, mobile mode), rebranded, with the Bangla edition,
the Studio library and the AI lyrics generator.

## Folder structure
```
.
├── vercel.json              Vercel config: static output public/, two Python functions, headers
├── requirements.txt         Python dependencies of the functions (faster-whisper only)
├── .python-version          3.12
├── api/
│   ├── health.py            GET  /api/health      (Vercel Python Function)
│   └── transcribe.py        POST /api/transcribe  (Vercel Python Function)
├── lyravox_ai/              the AI service (shared by api/*.py and the local dev server)
│   ├── core.py  web.py  wav.py  engines.py  lines.py  config.py  errors.py  audio.py  devserver.py
├── public/                  built site (committed; this is what Vercel serves)
├── src/  app/  site/        frontend sources (engine, editor UI, AI dialog app/aigen.js, site pages)
├── build.py                 python3 build.py  ->  public/
├── tests/                   api/ (stdlib unittest) + Playwright browser tests
├── scripts/eval_transcription.py   real-recording quality report (WER/CER/timestamps)
├── dev/                     original engine regression tools (860 components)
└── docs/                    VERCEL.md, API.md, TEST_REPORT.md, OFFLINE_TRANSCRIPTION.md
```

## Deploy on Vercel (one project)
1. Push to GitHub. 2. Vercel → **Add New Project** → import the repo. Framework **Other**, root directory `/`, **no build/install command, no environment variables** (all preset in `vercel.json`).
3. Deploy, then open `/api/health` → `"engine_available": true`. Details, limits and reasoning: [docs/VERCEL.md](docs/VERCEL.md).
No other hosting platform is needed. **No environment variable is required.**

## Run everything locally (pages + API on one port)
```bash
pip install -r requirements.txt              # faster-whisper (real transcription; the model downloads on first use)
python3 build.py                             # only if you changed sources; public/ is committed
python3 -m lyravox_ai.devserver              # http://localhost:8000  (same handler code as Vercel)
# without a model, to try the UI: LYRAVOX_ALLOW_FAKE=1 python3 -m lyravox_ai.devserver --fake   (TEST-ONLY placeholder text "TEST LINE n", labelled in the UI)
```

## AI Generate Lyrics
Upload a song and let AI detect the sung lyrics in **Bangla, English or Hindi** (auto-detect or manual), review them in a line editor, and send them with timestamps to the editor.
* **Where:** Studio → *Audio Library* (✨ on every song and in the toolbar), Studio → *Lyrics Studio* (*From audio*), and the editor's *Audio* section. Nothing to configure.
* **How it works:** your browser decodes the song and cuts it at quiet moments into ~30 s pieces (silent pieces are skipped), then sends them one by one to this site's own `/api/transcribe`; progress is real (piece *n* of *N*),
  **Cancel** stops sending, and **Retry** continues from the piece that failed. The server holds nothing between requests and never writes audio to disk.
* **Line editor:** edit text and times (↑/↓ nudge 0.1 s), split at the cursor, merge, add, delete, play a line or the whole song with the current line highlighted, *Regenerate selected lines*, reset, copy, `.lrc` download.
* **To the editor:** *Send all* (undoable with Ctrl+Z), `→` per line, *Send selected*. Timestamps become `[mm:ss.xx]` tags the engine uses as line starts; gaps become `[interlude N]`; characters with meaning in the lyric syntax are escaped.
* **Separate vocals first (Demucs):** kept, but **disabled on Vercel** with the reason (it cannot run in a function); requests for it are answered with `used: false`. It works on a self-hosted machine with `demucs` + ffmpeg.

### Known limitations
* Whisper is a speech model. **Singing over loud music is hard for it**; expect errors, missed or invented words, and clearly weaker Bangla and Hindi than English, especially with the `base` model that fits Vercel. Always review the lyrics. No Bangla/Hindi accuracy was measured in this project ([docs/TEST_REPORT.md](docs/TEST_REPORT.md)).
* The first request after a cold start downloads the model (~150 MB) into `/tmp` and is slow. Models that do not fit Vercel's 500 MB `/tmp` (`small` and up) are refused with a clear message instead of failing mid-download. Diagnostics: `GET /api/transcribe`. Verify a deployment: `python scripts/verify_production.py URL --bn … --en … --hi …`.
* Browser-side decoding needs a current Chrome, Edge, Safari or Firefox; very long songs use phone memory (the UI limit is 15 minutes).
* The public API has a best-effort per-instance rate limit; add Vercel Firewall rules for production ([docs/VERCEL.md](docs/VERCEL.md)).
* Not offline: see [docs/OFFLINE_TRANSCRIPTION.md](docs/OFFLINE_TRANSCRIPTION.md).

## Build
`python3 build.py` regenerates `public/` (Python 3 only, no npm). `public/` is committed so Vercel needs no build step.

## Tests
```bash
python3 -m unittest discover -s tests/api -p "test_*.py" -t tests/api     # API, validation, limits, Vercel config (no extra packages)
python3 build.py && python3 tests/ai_ui_test.py                           # browser: AI dialog + editor integration (starts its own servers)
python3 -m lyravox_ai.devserver --port 8800 &                              # then:
python3 tests/editor_bn.py ; python3 tests/export_test.py ; python3 tests/png_test.py ; python3 tests/studio_test.py
python3 build.py --dev && python3 dev/build_test.py all --all-packs       # then: python3 dev/smoke_all.py t_all   (860 components)
LYRAVOX_REAL=1 python3 -m unittest discover -s tests/api -p test_real_transcription.py -t tests/api   # real Whisper on your recordings
```

## Credits and license
Developer **Prince AR Abdur Rahman**, publisher **NexVora Lab's Ofc**, Bangladesh. Based on **JIZURA** by hakoniwa (MIT) — https://github.com/852wa/JIZURA. See `LICENSE`, `THIRD_PARTY_NOTICES.md`.
Privacy: editing, projects and exports never leave the browser; only the optional AI feature sends audio pieces to this site's own function, which keeps nothing.

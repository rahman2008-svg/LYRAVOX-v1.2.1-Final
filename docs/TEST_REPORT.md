# LYRAVOX 1.2.1 — Test report: model-storage fix ("No space left on device")

**Honest summary: the root cause is identified and the fix is implemented and tested, but it is NOT proven in production.** The development sandbox has no network, no `faster-whisper`,
no Whisper model and no Bangla / English / Hindi recordings, so **no real model download and no real transcription could be run here.** The production error is therefore *not* claimed fixed.
`scripts/verify_production.py` performs the real bn / en / hi test against your deployment; run it and trust its result, not this document.

## Root cause (from the code + the error text + Vercel docs)
* The message "RuntimeError: Task error: File reconstruction error: … No space left on device (os error 28)" is raised by **hf-xet** inside `huggingface_hub`'s model download (`xet_get` → `_download_to_tmp_and_move`) — i.e. while `faster-whisper` fetched `model.bin`.
* Old code: `WhisperModel(name, download_root="/tmp/lyravox-models")` + `HF_HOME=/tmp/lyravox-models/hf`. Xet's chunk cache (`HF_HOME/xet`) and the `.incomplete` reconstruction file are two copies of the model in `/tmp` (~2× ≈ 300 MB for `base`); Vercel allows **500 MB** of writable `/tmp` per function (Vercel docs, checked while fixing).
* No free-space check, no cleanup, and `_model` stayed `None` after a failure, so every request on a warm instance restarted the download on top of the previous leftovers until `/tmp` was full. `small` (~480 MB file) can never fit.

## What was executed (all real runs in the sandbox)
| Test | Result |
|---|---|
| `tests/api/test_modelstore.py` (21 tests) | **21 passed** |
| Whole API suite `python3 -W error::ResourceWarning -m unittest discover -s tests/api -p "test_*.py" -t tests/api` | **84 tests: 80 passed, 4 skipped, 0 failed** (skipped = the real-Whisper tests: no model/recordings here) |
| Browser suite `tests/ai_ui_test.py` | **61 / 61** (placeholder engine, as before) |
| Studio + mobile + Bangla editor regression | pass, no console errors |
| `scripts/verify_production.py` mechanics | against the placeholder engine → **FAILS by design**; against the genuinely missing engine → **FAILS with the 503 + diagnostics** (cannot give a false pass) |

What the 21 storage tests prove (REAL downloader, REAL HTTP to a local *mock* hub that imitates huggingface.co's URL scheme, REAL disk, REAL threads):
* only the 5 needed files are fetched, into **one** folder; disk use equals the model size (3 MB model → 3.0 MB), no hub/xet cache, no partial folders;
* second use, and a brand-new store object, make **zero** network requests; 8 concurrent requests cause **one** download;
* truncated download / corrupt JSON / size mismatch → partial data deleted, state `failed` with the stage, and a retry succeeds;
* a model larger than the budget, or not enough free space, is refused **before any file is requested** (and remembered: no more traffic);
* **real ENOSPC**: a real 1 MB tmpfs with a hub that under-reports the size (so the pre-check passes and the disk fills mid-download) → caught, `.partial` removed, the free space is really restored, stage `download`, reason `insufficient_storage`;
* leftovers of older attempts (`models--*`, `hf/xet`, other models, `.partial`) are swept; an operator-supplied model folder is used with no download and no `/tmp` use;
* with a MOCK `faster_whisper` module: the model is built **exactly once** (6 parallel requests, two transcribers) and from a **local folder**, never from a model name (so the library cannot download);
  a download failure returns `503 model_unavailable` with `stage` + `diagnostics`, builds no model and returns no text; the next request recovers; a library "No space left on device" error is translated to `503 … insufficient_storage` instead of a 500;
* `GET /api/transcribe` and `?warmup=1` through the real `api/transcribe.py` handler class.

## NOT run (required before calling the production problem fixed)
* **Real model download from huggingface.co** (the mock hub follows the documented URL scheme `/api/models/<repo>?blobs=true` and `/<repo>/resolve/main/<file>`; the real hub could differ in detail — e.g. if it returned no sizes the code falls back to HEAD requests and then to an estimate).
* **Real model loading, real transcription in Bangla (bn), English (en), Hindi (hi), real timestamps, real language detection.**
* A real Vercel deployment (`/tmp` behaviour on a warm vs cold instance, bundle size, cold-start time).
Run: `python scripts/verify_production.py https://YOUR-APP.vercel.app --bn bangla.mp3 --en english.mp3 --hi hindi.mp3` — it checks engine, download+load, script/timestamps per language, auto-detect, and that a second run neither re-downloads nor grows the cache.

## Remaining Vercel limitations
* `/tmp` ≤ 500 MB and not shared/persistent: **every cold instance downloads the model again** (slow first request). Only a model folder bundled in the deployment avoids it (`tiny` fits GitHub's 100 MB limit; `base` needs Git LFS — not tested).
* `small` and larger models cannot be used (refused with a clear message); Whisper `base`/`tiny` accuracy for Bangla and Hindi is limited.
* `/api/health` and `/api/transcribe` are separate functions with separate `/tmp`; use `GET /api/transcribe` for the diagnostics that matter.
* 4.5 MB request body, 300 s duration (Fluid compute), no PyTorch/Demucs — unchanged from 1.2.0.

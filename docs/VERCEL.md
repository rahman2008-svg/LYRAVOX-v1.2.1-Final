# One project, one deployment: how LYRAVOX runs on Vercel

```
GET /…                    static files from public/ (CDN)            built by `python3 build.py`, committed
GET /api/health           api/health.py       Python Function         engine / limits / Demucs status
POST /api/transcribe      api/transcribe.py   Python Function         ONE ~30 s WAV piece -> lyric lines (stateless)
```
Both functions are standard-library `BaseHTTPRequestHandler` classes (`handler`), the shape Vercel's Python runtime documents. All logic is in `lyravox_ai/`,
shared with the local dev server, so what is tested locally is the deployed code. Only `faster-whisper` is installed (`requirements.txt`).

## Why it is built this way (limits taken from Vercel's documentation, checked while building this)
| Vercel limit | Consequence for the design |
|---|---|
| Request/response bodies are limited to **4.5 MB** | A song cannot be uploaded in one request. The **browser decodes the song** (Web Audio) to 16 kHz mono, cuts it at quiet moments into pieces of about 30 s (≈ 1 MB of WAV) and sends them one by one. The server never sees mp3/m4a, needs no ffmpeg, and writes nothing to disk |
| Functions have **no memory between requests** | There is no job queue or job id. Each request is self-contained; the browser assembles the pieces, so progress, **cancel** (stop sending) and **retry/resume** (re-send only the failed piece) are client-side |
| Maximum duration **300 s (Hobby with Fluid compute)**, 800 s on Pro | A piece takes seconds to a minute, not the length of a song. The function stops itself at 270 s and answers 504 instead of being killed |
| Python bundle **500 MB uncompressed**, no apt packages | Only `faster-whisper` (CTranslate2, ONNX Runtime, PyAV) is installed. No PyTorch, no ffmpeg |
| Read-only filesystem; only `/tmp` is writable, **up to 500 MB** (Vercel docs) | The Whisper model is downloaded on the first request of a cold instance into `/tmp/lyravox-models` — see "Storage" below (`base` ≈ 150 MB fits; `small` ≈ 480 MB does not) |

## Demucs ("Separate vocals first") on Vercel: not possible, so it is reported honestly
Demucs needs PyTorch (installed size on the order of hundreds of MB, close to or over the 500 MB bundle limit together with faster-whisper), and separating a song on a
CPU is far slower than real time: a function limited to 300 s cannot do it reliably. This was **not** tried on Vercel (nothing can be deployed from the development environment);
it is a reasoned decision, not a measurement.
What the app does instead of pretending:
* `/api/health` reports `vocal_separation: {available: false, reason: "…Vercel…"}`; the checkbox stays visible but **disabled**, with the reason shown.
* If a client asks for it anyway, the response says `requested: true, used: false, error: "…"` and a warning; transcription runs on the original audio.
* On a host that has `demucs` and ffmpeg installed and is not Vercel (`python -m lyravox_ai.devserver` after `pip install demucs`), the same code path is available and used.

## Deploy (one click / one command)
1. Push this repository to GitHub. 2. Vercel → *Add New Project* → import it. **Framework preset: Other. Root directory: `/`. No build command, no install command, no environment variables.** (`vercel.json` already says so.)
3. Deploy. Open `https://<project>.vercel.app/api/health` — you should see `"engine_available": true`. The first transcription after a cold start is slow (model download); later ones are fast.
CLI: `npm i -g vercel && vercel --prod`.

If the deploy fails with a maxDuration error, your project has Fluid compute off: enable it in the project settings (Functions) or lower `maxDuration` in `vercel.json` to 60 and set `CHUNK_SECONDS=15`.

## Environment variables — none are required
Everything works with the defaults. Optional tuning (Project → Settings → Environment Variables):
| variable | default | purpose |
|---|---|---|
| `WHISPER_MODEL` | `base` | `tiny` (faster, weaker) or `base`. `small` and larger do not fit `/tmp` and are refused with a clear message. May also be a folder containing `model.bin` (see "Storage") |
| `MODEL_CACHE_BUDGET_MB` | `400` on Vercel | most `/tmp` the model may use; a model that needs more is refused before anything is downloaded |
| `CHUNK_SECONDS` | `30` | piece length the browser uses (smaller = shorter requests) |
| `MAX_DURATION_S` | `900` | longest song the UI accepts |
| `RATE_LIMIT_PER_MIN` | `90` | best-effort requests per minute per client address, per instance |
| `REQUEST_TIMEOUT_S` | `270` on Vercel | self-imposed limit per request |
| `WHISPER_BEAM` | `1` | beam size (1 = fastest) |
| `ALLOWED_LANGUAGES` | `bn,en,hi` | offered languages |

## Abuse and cost
The API is public and unauthenticated by design (no accounts, no keys). It refuses requests that browsers mark as cross-site, and rate limits per client address, but the limiter is
**in memory per instance**, so it is best effort, not a security boundary. Anyone can call `/api/transcribe` directly with curl and use your function time. For production add Vercel's
Firewall / rate-limiting rules for `/api/transcribe` (Project → Firewall) and watch usage.

## Storage: the "No space left on device (os error 28)" failure, and the fix (v1.2.1)
**Root cause.** `WhisperModel("base", download_root="/tmp/…")` makes `faster-whisper` call `huggingface_hub`, which (current versions) downloads `model.bin` with **hf-xet**.
Xet keeps a chunk cache under `HF_HOME` *and* reconstructs the file into a second `.incomplete` copy, so a ~150 MB model needs about twice that in `/tmp`. The old code set `HF_HOME`
into `/tmp` too, never checked free space, never cleaned a failed attempt, and kept no memory of the failure: every later request on the same warm instance started the download again on top
of the leftovers until the 500 MB `/tmp` was full. hf-xet reports this as `RuntimeError: Task error: File reconstruction error: IO Error: No space left on device (os error 28)`.
A larger model (`small` ≈ 480 MB) can never fit.

**Fix** (`lyravox_ai/modelstore.py`): one folder, one copy, plain streaming HTTPS (no hf-xet, no hub cache), free-space + budget check *before* downloading (fail fast, nothing written),
download into `<model>.partial` and atomic rename only when all sizes match, `try/finally` removal of partial data on every failure (including ENOSPC), a sweep of leftovers/other models first,
a single-flight lock (concurrent requests never download twice), one `WhisperModel` per process, and `HF_HUB_DISABLE_XET=1` as defence in depth.
A model that is already complete on disk is used without any network access. Errors are `503 model_unavailable` with `stage` (`resolve|disk_check|download|verify|load|transcribe`), `reason`
and `diagnostics`; nothing is ever replaced by placeholder text.

**Diagnostics.** On Vercel `/api/health` and `/api/transcribe` are two functions with **separate `/tmp`**, so ask the one that transcribes:
`GET /api/transcribe` → disk free/total, cache size and budget, model state, failure stage, downloads this process; `GET /api/transcribe?warmup=1` additionally downloads + loads the model.

**Still true on Vercel.** Each *cold* instance downloads the model again (`/tmp` is not shared or persistent) — first request after idle is slow. To avoid the download and all `/tmp` use, put a model folder
(`model.bin`, `config.json`, `tokenizer.json`, `vocabulary.*`, `preprocessor_config.json`) inside the deployment and set `WHISPER_MODEL=./models/tiny` (read-only access is enough). GitHub rejects files over 100 MB
without Git LFS, so only `tiny` (~75 MB) can be committed normally. This path is supported by the code but was not deployed.

**Verify on your deployment** with real recordings: `python scripts/verify_production.py https://YOUR-APP.vercel.app --bn bn.mp3 --en en.mp3 --hi hi.mp3`.

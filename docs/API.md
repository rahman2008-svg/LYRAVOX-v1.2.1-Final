# LYRAVOX AI — API (same origin as the site)

Base: the same address as the pages (`https://<project>.vercel.app/api/…`, or `http://localhost:8000/api/…` with the dev server). No keys, no auth, no CORS: the API is for the site's own pages
(requests that browsers mark cross-site are refused with 403). All responses are JSON with `Cache-Control: no-store`. Errors always look like

```json
{ "error": { "code": "engine_unavailable", "message": "…" } }
```

## `GET /api/health`
```json
{
  "status": "ok", "version": "1.2.0", "engine": "faster-whisper", "engine_available": true, "engine_reason": null, "model": "base", "model_loaded": false,
  "languages": ["bn", "en", "hi"], "runtime": "vercel",
  "vocal_separation": { "available": false, "reason": "Vocal separation (Demucs) cannot run on Vercel: …" },
  "limits": { "max_request_bytes": 4400000, "chunk_seconds": 30, "max_chunk_seconds": 120, "max_duration_s": 900, "request_timeout_s": 270, "rate_limit_per_min": 90 },
  "audio_format": { "container": "wav", "codec": "pcm_s16le", "sample_rate": 16000, "channels": 1 }
}
```
`status` is `degraded` and `engine_available` false when the speech engine is not installed; `engine_reason` says why. The UI shows that reason and disables *Generate*.

## `GET /api/transcribe` — diagnostics of the transcribing function
`200 {"status": "ok", "diagnostics": {…}}` with `name`, `source` (`hub`|`local-dir`), `state` (`idle|resolving|downloading|ready|failed`), `stage`, `failure`, `loaded`, `load_ms`, `downloads_this_process`,
`disk {free_mb,total_mb,used_mb,path}`, `cache {size_mb,budget_mb,path}`, `files`, `runtime`. `?warmup=1` downloads and loads the model first (`503` with the error below if that fails).
`/api/health` carries the same `diagnostics` for its own process (on Vercel a different function with its own `/tmp`).

## `POST /api/transcribe` — one piece of audio
Body: **raw WAV bytes** (`Content-Type: audio/wav`), mono, 16 kHz, 16-bit PCM, at most `max_chunk_seconds` long and `max_request_bytes` large. `Content-Length` is required.
Query parameters:

| parameter | default | meaning |
|---|---|---|
| `language` | `auto` | `auto` or one of `languages`. `auto` detects among the supported languages and reports it; send the detected language for later pieces |
| `offset` | `0` | seconds added to every returned time, so pieces of one song share one timeline |
| `word_timestamps` | `1` | per-word timings (used to split lines at real pauses) |
| `vocal_separation` | `0` | request Demucs. Where it is unavailable the response says so (see below) and the original audio is used |

Response `200`:
```json
{
  "language": "bn", "language_probability": 0.93, "language_detected": true, "language_probabilities": { "bn": 0.93, "hi": 0.05 },
  "duration": 29.98, "offset": 30.0, "engine": "faster-whisper", "model": "base", "skipped_silence": false,
  "segments": [
    { "id": 1, "start": 41.2, "end": 44.6, "text": "…", "confidence": 0.81, "approx_timing": false,
      "words": [ { "start": 41.2, "end": 41.7, "text": "…", "confidence": 0.9 } ] }
  ],
  "vocal_separation": { "requested": true, "used": false, "error": "Vocal separation (Demucs) cannot run on Vercel: …" },
  "warnings": [ "…" ]
}
```
* `skipped_silence: true` — the piece is silent / below the noise threshold: no model was run (the browser also skips such pieces without sending them).
* `confidence` — heuristic 0–1 from Whisper's log-probabilities; not a calibrated probability of being right.
* `vocal_separation.used` is `true` only if separation really ran; `error` explains why not.

Errors:
| status | code | when |
|---|---|---|
| 400 | `incomplete_body` | body shorter than Content-Length |
| 403 | `cross_site_blocked` | request marked cross-site by the browser (`Sec-Fetch-Site` / `Origin`) |
| 411 | `length_required` | no Content-Length |
| 413 | `payload_too_large` / `chunk_too_long` | over `max_request_bytes` / longer than `max_chunk_seconds` |
| 415 | `unsupported_type` | Content-Type is not audio/wav |
| 422 | `invalid_audio` / `empty_audio` / `audio_too_short` / `unsupported_language` / `invalid_parameter` | not a mono 16 kHz 16-bit PCM WAV, empty, < 0.3 s, bad parameter |
| 429 | `rate_limited` | too many requests from this client (`Retry-After` header) |
| 500 | `transcription_failed` | the engine raised an error (message included) |
| 503 | `engine_unavailable` / `model_unavailable` / `server_busy` / `out_of_memory` | engine not installed; model could not be prepared (the error also carries `stage`, `reason` — e.g. `model_too_large`, `insufficient_storage`, `hub_unreachable`, `size_mismatch`, `model_load_failed` — and `diagnostics`); another piece is being processed too long; memory |
| 504 | `timeout` | the piece took longer than `request_timeout_s` |

## How the browser uses it
1. `GET /api/health` (enables the dialog, reads limits and the Demucs status).
2. Decode the song locally to 16 kHz mono; cut it into pieces of about `chunk_seconds`, at quiet moments, never longer than `max_chunk_seconds`; skip silent pieces.
3. `POST` the pieces one after another (first with `language=auto` if auto-detect is chosen, then with the detected language). Failed requests are retried twice (honouring `Retry-After`); if they still fail the dialog offers *Retry*, which continues with the failed piece and keeps the finished ones.
4. Merge the segments (already on the song's timeline through `offset`), compute instrumental gaps, show the editable lines.
Cancel simply stops sending and aborts the request in flight.

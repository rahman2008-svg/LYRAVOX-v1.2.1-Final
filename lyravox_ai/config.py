"""Configuration, read from environment variables. NONE is required: every value has a working default, and there are no secrets."""
import os
from dataclasses import dataclass, field


def _int(name, default):
    try: return int(os.environ.get(name, default))
    except ValueError: return default


def _float(name, default):
    try: return float(os.environ.get(name, default))
    except ValueError: return default


def _list(name, default):
    return [x.strip() for x in os.environ.get(name, default).split(',') if x.strip()]


def on_vercel():
    return bool(os.environ.get('VERCEL') or os.environ.get('VERCEL_ENV'))


@dataclass
class Config:
    # Vercel rejects request bodies over 4.5 MB (4,718,592 bytes), so every chunk of audio must fit well under it
    max_request_bytes: int = 4_400_000
    chunk_s: int = 30                      # the browser sends the song in pieces of about this length (cut at quiet moments)
    max_chunk_s: int = 120                 # hard server limit for one piece
    max_duration_s: int = 900              # longest song the UI accepts (enforced in the browser, reported by /api/health)
    request_timeout_s: float = 270.0       # stop a request before the platform does (Vercel: 300 s on Hobby with Fluid compute)
    rate_limit_per_min: int = 90           # per client address, best effort (see docs/VERCEL.md)
    whisper_model: str = 'base'            # tiny | base | ... or a folder containing model.bin ('small' and up do not fit Vercel's 500 MB /tmp)
    whisper_device: str = 'cpu'
    whisper_compute: str = 'int8'
    whisper_beam: int = 1                  # 1 = fastest; raise on a fast host
    model_cache: str = ''                  # folder for the downloaded model ('' = /tmp/lyravox-models on Vercel, ~/.cache/lyravox/models elsewhere)
    model_budget_mb: int = 0               # most storage the model may use (Vercel: 400 of the 500 MB /tmp); 0 = only the free-space check
    model_margin_mb: int = 40              # headroom kept free beyond the model size
    languages: list = field(default_factory=lambda: ['bn', 'en', 'hi'])
    demucs_enabled: bool = True            # honoured only where Demucs can run (never on Vercel)
    demucs_model: str = 'htdemucs'
    demucs_timeout_s: int = 600
    interlude_min_s: float = 6.0
    silence_rms: float = 0.004             # a piece quieter than this (RMS, full scale = 1.0) is treated as silence and not sent to the model

    @classmethod
    def from_env(cls):
        d = cls()
        d.max_request_bytes = _int('MAX_REQUEST_BYTES', d.max_request_bytes)
        d.chunk_s = max(5, _int('CHUNK_SECONDS', d.chunk_s))
        d.max_chunk_s = max(d.chunk_s, _int('MAX_CHUNK_SECONDS', d.max_chunk_s))
        d.max_duration_s = _int('MAX_DURATION_S', d.max_duration_s)
        d.request_timeout_s = _float('REQUEST_TIMEOUT_S', 270.0 if on_vercel() else 900.0)
        d.rate_limit_per_min = _int('RATE_LIMIT_PER_MIN', d.rate_limit_per_min)
        d.whisper_model = os.environ.get('WHISPER_MODEL', d.whisper_model)
        d.whisper_device = os.environ.get('WHISPER_DEVICE', d.whisper_device)
        d.whisper_compute = os.environ.get('WHISPER_COMPUTE', d.whisper_compute)
        d.whisper_beam = _int('WHISPER_BEAM', d.whisper_beam)
        d.model_cache = os.environ.get('WHISPER_CACHE', '/tmp/lyravox-models' if on_vercel() else '')
        d.model_budget_mb = _int('MODEL_CACHE_BUDGET_MB', 400 if on_vercel() else 0)
        d.model_margin_mb = _int('MODEL_MARGIN_MB', d.model_margin_mb)
        d.languages = _list('ALLOWED_LANGUAGES', ','.join(d.languages))
        d.demucs_enabled = os.environ.get('DEMUCS_ENABLED', '1').strip().lower() in ('1', 'true', 'yes', 'on')
        d.demucs_model = os.environ.get('DEMUCS_MODEL', d.demucs_model)
        d.interlude_min_s = _float('INTERLUDE_MIN_S', d.interlude_min_s)
        return d

    @property
    def max_chunk_bytes(self):
        return min(self.max_request_bytes, self.max_chunk_s * 32000 + 4096)       # 16 kHz * 16 bit * mono = 32000 B/s

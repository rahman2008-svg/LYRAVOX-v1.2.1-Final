"""Strict reader for the one audio format the API accepts: mono, 16 kHz, 16-bit PCM WAV (what the browser produces).
No ffmpeg, no audio library: the server never decodes mp3 / m4a itself, so the function stays small and nothing is written to disk."""
import struct
from .errors import ApiError

RATE, CHANNELS, BITS = 16000, 1, 16


def parse(body):
    """Return (numpy float32 samples in [-1, 1], duration seconds). Raises ApiError(422/413) with a clear message."""
    import numpy as np
    if len(body) < 44 or body[:4] != b'RIFF' or body[8:12] != b'WAVE':
        raise ApiError(422, 'invalid_audio', 'The request body is not a WAV file (expected audio/wav: mono, 16 kHz, 16-bit PCM).')
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(body):
        cid, size = body[pos:pos + 4], struct.unpack('<I', body[pos + 4:pos + 8])[0]
        start = pos + 8
        if cid == b'fmt ':
            if size < 16 or start + 16 > len(body):
                raise ApiError(422, 'invalid_audio', 'Damaged WAV header.')
            fmt = struct.unpack('<HHIIHH', body[start:start + 16])
        elif cid == b'data':
            data = body[start:start + size] if size != 0xFFFFFFFF else body[start:]
            break
        pos = start + size + (size & 1)
    if fmt is None or data is None:
        raise ApiError(422, 'invalid_audio', 'Damaged WAV file (no fmt or data section).')
    tag, ch, rate, _, _, bits = fmt
    if tag != 1 or ch != CHANNELS or rate != RATE or bits != BITS:
        raise ApiError(422, 'invalid_audio', f'Unsupported WAV format (format={tag}, channels={ch}, rate={rate}, bits={bits}). Send mono, 16000 Hz, 16-bit PCM.')
    if len(data) % 2:
        data = data[:-1]
    if len(data) == 0:
        raise ApiError(422, 'empty_audio', 'The WAV file contains no audio.')
    pcm = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0
    return pcm, len(pcm) / RATE


def encode(samples_f32):
    """Float samples -> WAV bytes (used by tests and by the evaluation script)."""
    import numpy as np
    pcm = (np.clip(samples_f32, -1.0, 1.0) * 32767.0).astype('<i2').tobytes()
    return b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 1, RATE, RATE * 2, 2, 16) + b'data' + struct.pack('<I', len(pcm)) + pcm

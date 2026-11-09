"""Test helpers. Audio is generated with numpy / ffmpeg. The 'speech' is tone bursts separated by silence — NOT speech — so tests that use it check
audio handling, validation, chunk plumbing and timestamps, never recognition quality."""
import importlib.util, os, subprocess, sys, tempfile, shutil, threading
os.environ.setdefault('LYRAVOX_ALLOW_FAKE', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import numpy as np
from lyravox_ai import wav
from lyravox_ai.config import Config
from lyravox_ai.core import Core, set_core
from lyravox_ai.engines import FakeTestTranscriber

ENCODERS = {
    'wav': ['-c:a', 'pcm_s16le'], 'mp3': ['-c:a', 'libmp3lame', '-b:a', '64k'], 'ogg': ['-c:a', 'libvorbis', '-q:a', '2'],
    'm4a': ['-c:a', 'aac', '-b:a', '64k'], 'aac': ['-c:a', 'aac', '-b:a', '64k', '-f', 'adts'],
}


def tmpdir():
    return tempfile.mkdtemp(prefix='lvtest-')


def bursts(seconds=10.0, on=3.0, off=2.0, amp=0.5, rate=16000):
    """float32 tone bursts: `on` seconds of sound, `off` seconds of silence, repeating."""
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 330 * t) * amp * ((t % (on + off)) < on)).astype(np.float32)


def wav_bytes(seconds=10.0, on=3.0, off=2.0, amp=0.5):
    return wav.encode(bursts(seconds, on, off, amp))


def make_audio_file(path, seconds=10.0, on=3.0, off=2.0, rate=16000):
    """Real encoded file through ffmpeg (for browser decode tests)."""
    fmt = os.path.splitext(path)[1][1:]
    expr = f"sin(2*PI*330*t)*0.6*lt(mod(t\\,{on + off})\\,{on})"
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'aevalsrc={expr}:d={seconds}:s={rate}', '-ac', '1', '-ar', str(rate)] + ENCODERS[fmt] + [path], check=True)
    return path


def make_core(delay=0.0, fail_first=0, **cfg_kw):
    cfg = Config()
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    eng = FakeTestTranscriber(cfg, delay=delay, fail_first=fail_first)
    return Core(cfg, eng), cfg, eng


def load_handler(name):
    """Import api/<name>.py exactly as Vercel does and return its `handler` class."""
    spec = importlib.util.spec_from_file_location('api_' + name, os.path.join(ROOT, 'api', name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handler


def serve_handler(handler_cls):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_address[1]}'


make_audio = make_audio_file


def encoder_available(fmt):
    d = tmpdir()
    try:
        make_audio_file(os.path.join(d, 't.' + fmt), 1.0, 0.5, 0.2)
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)

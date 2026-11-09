"""Safe, single-copy storage for the Whisper model files.

WHY THIS EXISTS (production error "RuntimeError: Task error: File reconstruction error: IO Error: No space left on device (os error 28)"):
`WhisperModel("base", download_root="/tmp/...")` hands the download to huggingface_hub, which on current versions fetches model.bin with
hf-xet. Xet keeps a chunk cache (under HF_HOME) AND reconstructs the file into a second ".incomplete" copy, so a ~145 MB model needs roughly twice
that in /tmp, nothing checked free space, a failed attempt left its leftovers behind and the next request started over on top of them, until
Vercel's 500 MB /tmp was full. A "small" model (~480 MB file) could never fit.

WHAT THIS DOES INSTEAD
* downloads only the files faster-whisper needs, with plain HTTPS streaming (no hf-xet, no hub cache, no second copy), straight into ONE folder;
* checks free space and a storage budget BEFORE downloading and fails fast with a clear message (a model that cannot fit is never attempted);
* downloads into "<model>.partial" and renames it only when every file has the expected size, so a half-downloaded model is never used;
* try/finally removes partial data on every failure (including ENOSPC) and sweeps leftovers of older attempts / other models first;
* downloads at most once per process (single-flight lock); a model that is already complete on disk is used without any network access;
* a model folder you provide yourself (WHISPER_MODEL=/path/to/dir containing model.bin) is used as is: no download, no /tmp use;
* records the stage in which something failed and exposes disk / cache / model status for diagnostics.
"""
import errno, fnmatch, json, os, shutil, tempfile, threading, time, urllib.error, urllib.request
from .errors import ModelError

def hf_endpoint():
    return os.environ.get('LYRAVOX_HF_ENDPOINT', 'https://huggingface.co').rstrip('/')       # overridable (tests use a local mock hub)


PATTERNS = ('config.json', 'preprocessor_config.json', 'model.bin', 'tokenizer.json', 'vocabulary.*')       # what faster-whisper loads
REPOS = {n: f'Systran/faster-whisper-{n}' for n in ('tiny', 'tiny.en', 'base', 'base.en', 'small', 'small.en', 'medium', 'medium.en', 'large-v1', 'large-v2', 'large-v3')}
REPOS['large'] = REPOS['large-v3']
APPROX_MB = {'tiny': 80, 'tiny.en': 80, 'base': 150, 'base.en': 150, 'small': 490, 'small.en': 490, 'medium': 1500, 'medium.en': 1500}      # only used if the hub gives no sizes
MARKER = '.lyravox-complete.json'
MB = 1024 * 1024


def prepare_env(root):
    """Keep every Hugging Face library off the Xet path and inside our folder, BEFORE any of them is imported."""
    os.environ['HF_HUB_DISABLE_XET'] = '1'                       # documented switch: plain HTTP downloads even if hf-xet is installed
    os.environ.setdefault('HF_HOME', os.path.join(root, 'hf'))
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    os.environ.setdefault('HF_HUB_ENABLE_HF_TRANSFER', '0')


def _existing_parent(path):
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path or '.'


def disk_info(path):
    try:
        u = shutil.disk_usage(_existing_parent(path))
        return {'path': path, 'free_mb': round(u.free / MB, 1), 'total_mb': round(u.total / MB, 1), 'used_mb': round(u.used / MB, 1)}
    except OSError as e:
        return {'path': path, 'error': str(e)}


def dir_size_mb(path):
    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            try: total += os.path.getsize(os.path.join(dp, f))
            except OSError: pass
    return round(total / MB, 1)


class ModelStore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg.whisper_model
        self.own_dir = os.path.isdir(self.name)                  # a model folder supplied by the operator (e.g. bundled with the deployment)
        self.root = cfg.model_cache or os.path.join(os.path.expanduser('~'), '.cache', 'lyravox', 'models')
        safe = self.name.replace('/', '--').replace(os.sep, '--')
        self.target = os.path.join(self.root, 'model-' + safe)
        self.lock = threading.Lock()                              # single-flight: concurrent requests never download twice
        self.state = 'idle'                                       # idle | resolving | downloading | ready | failed
        self.stage = None                                         # resolve | disk_check | download | verify | load
        self.failure = None
        self.path = None
        self.files = []
        self.downloads = 0                                        # how many times this process downloaded the model (diagnostics / tests)
        self._fatal = None                                        # a deterministic failure (does not fit) is remembered, not retried over the network

    # -- diagnostics ---------------------------------------------------------------------------------------------------
    def status(self):
        return {'name': self.name, 'source': 'local-dir' if self.own_dir else 'hub', 'state': self.state, 'stage': self.stage, 'failure': self.failure,
                'path': self.path, 'files': self.files, 'downloads_this_process': self.downloads,
                'disk': disk_info(self.root), 'cache': {'path': self.root, 'size_mb': dir_size_mb(self.root) if os.path.isdir(self.root) else 0.0,
                                                        'budget_mb': self.cfg.model_budget_mb or None}}

    def _fail(self, stage, reason, message, fatal=False):
        self.state, self.stage, self.failure = 'failed', stage, {'stage': stage, 'reason': reason, 'message': message}
        err = ModelError(stage, reason, message, self.status())
        if fatal:
            self._fatal = err
        return err

    # -- public --------------------------------------------------------------------------------------------------------
    def ensure(self):
        """Return the folder with the model files, downloading them once if necessary. Raises ModelError."""
        with self.lock:
            if self.path and self._complete(self.path):
                return self.path
            if self._fatal:
                raise self._fatal
            if self.own_dir:
                if not os.path.isfile(os.path.join(self.name, 'model.bin')):
                    raise self._fail('verify', 'model_files_missing', f"WHISPER_MODEL points to '{self.name}', but there is no model.bin in it.", fatal=True)
                self.path, self.state, self.stage = self.name, 'ready', None
                return self.path
            os.makedirs(self.root, exist_ok=True)
            self._sweep()
            if self._complete(self.target):
                self.path, self.state, self.stage, self.failure = self.target, 'ready', None, None
                return self.path
            self._download()
            return self.path

    # -- internals -----------------------------------------------------------------------------------------------------
    def _complete(self, d):
        try:
            with open(os.path.join(d, MARKER)) as f:
                m = json.load(f)
            return all(os.path.getsize(os.path.join(d, n)) == s for n, s in m['files'].items() if s is not None) and os.path.isfile(os.path.join(d, 'model.bin'))
        except (OSError, ValueError, KeyError):
            return False

    def _sweep(self):
        """Remove leftovers that only waste space: failed partial downloads, hub / xet caches of older attempts, other models."""
        try:
            for n in os.listdir(self.root):
                p = os.path.join(self.root, n)
                if p == self.target:
                    continue
                if n.endswith('.partial') or n in ('hf', 'hub', 'xet', '.cache') or n.startswith('models--') or n.startswith('model-'):
                    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass

    def _http(self, url, method='GET'):
        req = urllib.request.Request(url, method=method, headers={'User-Agent': 'lyravox-model-fetch/1.2'})
        return urllib.request.urlopen(req, timeout=30)

    def _list(self, repo):
        """[(filename, size_or_None)] of the files faster-whisper needs."""
        try:
            with self._http(f'{hf_endpoint()}/api/models/{repo}?blobs=true') as r:
                info = json.loads(r.read().decode('utf-8'))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise self._fail('resolve', 'hub_unreachable', f'Could not read the model list for {repo} from the model hub: {type(e).__name__}: {e}')
        out = []
        for s in info.get('siblings', []):
            n = s.get('rfilename', '')
            if any(fnmatch.fnmatch(n, p) for p in PATTERNS):
                size = s.get('size') or (s.get('lfs') or {}).get('size')
                if size is None:
                    try:
                        with self._http(f'{hf_endpoint()}/{repo}/resolve/main/{n}', 'HEAD') as r:
                            size = int(r.headers.get('X-Linked-Size') or r.headers.get('Content-Length') or 0) or None
                    except (urllib.error.URLError, OSError, ValueError):
                        size = None
                out.append((n, int(size) if size else None))
        if not any(n == 'model.bin' for n, _ in out):
            raise self._fail('resolve', 'model_not_found', f"The model repository {repo} has no model.bin.", fatal=True)
        return out

    def _download(self):
        cfg = self.cfg
        self.state, self.stage, self.failure = 'resolving', 'resolve', None
        if '/' in self.name:
            repo = self.name
        elif self.name in REPOS:
            repo = REPOS[self.name]
        else:
            raise self._fail('resolve', 'unknown_model', f"Unknown model '{self.name}'. Use tiny, base, small, medium, large-v3, a Hugging Face repo id, or a folder containing model.bin.", fatal=True)
        files = self._list(repo)
        need = sum(s if s is not None else APPROX_MB.get(self.name, 200) * MB for n, s in files if n == 'model.bin') + sum(s or 0 for n, s in files if n != 'model.bin')
        self.stage = 'disk_check'
        margin = cfg.model_margin_mb * MB
        budget = cfg.model_budget_mb * MB
        if budget and need + margin > budget:
            raise self._fail('disk_check', 'model_too_large',
                             f"The '{self.name}' model needs about {need // MB} MB, but only {max(0, (budget - margin)) // MB} MB of temporary storage may be used for it "
                             f"(Vercel gives a function at most 500 MB of /tmp). Use a smaller model (WHISPER_MODEL=base or tiny), or put the model folder in the deployment and point WHISPER_MODEL at it. Nothing was downloaded.", fatal=True)
        free = shutil.disk_usage(self.root).free
        if need + margin > free:
            raise self._fail('disk_check', 'insufficient_storage',
                             f"Not enough free storage for the '{self.name}' model: it needs about {need // MB} MB (+{cfg.model_margin_mb} MB margin) but only {free // MB} MB is free in {self.root}. Nothing was downloaded.")
        self.state, self.stage = 'downloading', 'download'
        part = self.target + '.partial'
        shutil.rmtree(part, ignore_errors=True)
        os.makedirs(part)
        deadline = time.monotonic() + max(60.0, cfg.request_timeout_s - 30)
        sizes = {}
        try:
            for n, expected in files:
                dest = os.path.join(part, n)
                written = 0
                try:
                    with self._http(f'{hf_endpoint()}/{repo}/resolve/main/{n}') as r, open(dest, 'wb') as out:
                        while True:
                            chunk = r.read(1 * MB)
                            if not chunk:
                                break
                            out.write(chunk); written += len(chunk)
                            if time.monotonic() > deadline:
                                raise self._fail('download', 'timeout', f'Downloading {n} took too long ({written // MB} MB received).')
                except ModelError:
                    raise
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        raise self._fail('download', 'insufficient_storage',
                                         f'Ran out of storage while downloading {n} ({written // MB} of {(expected or 0) // MB} MB written; {shutil.disk_usage(self.root).free // MB} MB free). The partial download was deleted.')
                    raise self._fail('download', 'download_failed', f'Downloading {n} failed after {written // MB} MB: {type(e).__name__}: {e}')
                self.stage = 'verify'
                if expected is not None and written != expected:
                    raise self._fail('verify', 'size_mismatch', f'{n} is {written} bytes but the hub says {expected}: the download was cut short. The partial download was deleted.')
                if n.endswith('.json'):
                    try:
                        with open(dest, encoding='utf-8') as f: json.load(f)
                    except ValueError:
                        raise self._fail('verify', 'corrupt_file', f'{n} is not valid JSON; the download is corrupt and was deleted.')
                sizes[n] = written
                self.stage = 'download'
            with open(os.path.join(part, MARKER), 'w') as f:
                json.dump({'model': self.name, 'repo': repo, 'files': sizes, 'time': time.time()}, f)
            shutil.rmtree(self.target, ignore_errors=True)
            os.rename(part, self.target)                     # atomic: a half-downloaded model can never be picked up
            self.path, self.files, self.state, self.stage, self.failure = self.target, sorted(sizes), 'ready', None, None
            self.downloads += 1
        finally:
            shutil.rmtree(part, ignore_errors=True)         # no-op after the rename; on ANY failure the partial data is gone

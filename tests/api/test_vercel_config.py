"""Static validation of the Vercel setup (vercel.json, entrypoints, runtime pin, requirements, output). This checks our configuration against the documented
rules; it is NOT the Vercel CLI and does not deploy anything."""
import ast, json, os, unittest
from http.server import BaseHTTPRequestHandler
from helpers import ROOT, load_handler


class VercelConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, 'vercel.json')) as f: cls.v = json.load(f)

    def test_single_project_static_plus_python_functions(self):
        v = self.v
        self.assertEqual(v.get('outputDirectory'), 'public'); self.assertTrue(os.path.isfile(os.path.join(ROOT, 'public', 'index.html')))
        self.assertIsNone(v.get('framework')); self.assertIsNone(v.get('buildCommand'))                     # the committed public/ is the build output
        self.assertFalse(os.path.exists(os.path.join(ROOT, 'package.json')), 'a package.json would make Vercel run an npm build')
        self.assertNotIn('rewrites', v); self.assertNotIn('routes', v)                                       # /api/<file> maps straight to api/<file>.py

    def test_every_configured_function_exists_and_is_a_handler(self):
        fns = self.v['functions']; self.assertEqual(set(fns), {'api/health.py', 'api/transcribe.py'})
        found = sorted(f for f in os.listdir(os.path.join(ROOT, 'api')) if f.endswith('.py')); self.assertEqual(found, ['health.py', 'transcribe.py'])
        for name in ('health', 'transcribe'):
            cls = load_handler(name)
            self.assertTrue(issubclass(cls, BaseHTTPRequestHandler)); self.assertEqual(cls.__name__, 'handler')                # the name the Python runtime looks for
            for m in ('do_GET', 'do_POST'): self.assertTrue(hasattr(cls, m))

    def test_durations_within_documented_limits(self):
        for path, cfg in self.v['functions'].items():
            self.assertLessEqual(cfg['maxDuration'], 300, path)               # Hobby with Fluid compute: 300 s (Pro: 800 s)
            self.assertGreaterEqual(cfg['maxDuration'], 5)
        self.assertLess(Core_default_timeout(), self.v['functions']['api/transcribe.py']['maxDuration'])    # we stop before the platform does

    def test_excludes_do_not_remove_code_the_functions_need(self):
        for path, cfg in self.v['functions'].items():
            ex = cfg['excludeFiles']
            for needed in ('api/', 'lyravox_ai/', 'requirements'):
                self.assertNotIn(needed, ex, f'{path} excludes {needed}')
            for big in ('public/**', 'src/**', 'vendor/**'):
                self.assertIn(big, ex)

    def test_python_pin_and_requirements(self):
        with open(os.path.join(ROOT, '.python-version')) as f: ver = f.read().strip()
        self.assertIn(ver, ('3.12', '3.13', '3.11'))
        with open(os.path.join(ROOT, 'requirements.txt')) as f: reqs = [l.strip() for l in f if l.strip() and not l.startswith('#')]
        self.assertEqual([r.split('>')[0].split('=')[0].split('<')[0] for r in reqs], ['faster-whisper'])           # nothing else (bundle limit 500 MB)
        for forbidden in ('torch', 'demucs', 'fastapi', 'uvicorn'): self.assertNotIn(forbidden, ' '.join(reqs))

    def test_function_source_is_tiny_and_imports_only_the_standard_library_at_import_time(self):
        total = sum(os.path.getsize(os.path.join(d, f)) for d in (os.path.join(ROOT, 'api'), os.path.join(ROOT, 'lyravox_ai')) for f in os.listdir(d) if f.endswith('.py'))
        self.assertLess(total, 200_000)
        heavy = {'faster_whisper', 'numpy', 'torch', 'ctranslate2'}
        for fn in ('api/health.py', 'api/transcribe.py', 'lyravox_ai/web.py', 'lyravox_ai/errors.py'):
            with open(os.path.join(ROOT, fn)) as fh: tree = ast.parse(fh.read())
            tops = {n.names[0].name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            self.assertFalse(tops & heavy, f'{fn} imports {tops & heavy} at import time (cold start / health must stay light)')

    def test_security_headers_present(self):
        keys = {h['key'] for rule in self.v['headers'] for h in rule['headers']}
        self.assertTrue({'X-Content-Type-Options', 'Referrer-Policy', 'Permissions-Policy'} <= keys)

    def test_no_hardcoded_server_addresses_in_shipped_files(self):
        bad = []
        for base in ('public', 'app', 'src', 'site', 'lyravox_ai', 'api'):
            for dp, _, fs in os.walk(os.path.join(ROOT, base)):
                for f in fs:
                    if f.endswith(('.js', '.html', '.py', '.css', '.json', '.webmanifest')) and f != 'devserver.py':      # the local dev server may mention its own address
                        with open(os.path.join(dp, f), encoding='utf-8', errors='ignore') as fh: txt = fh.read()
                        for needle in ('localhost:8000', 'onrender.com', 'No AI server is configured', 'LYRAVOX_API_URL', 'lyravox.api'):
                            if needle in txt: bad.append((os.path.relpath(os.path.join(dp, f), ROOT), needle))
        self.assertEqual(bad, [])


def Core_default_timeout():
    os.environ['VERCEL'] = '1'
    try:
        from lyravox_ai.config import Config
        return Config.from_env().request_timeout_s
    finally:
        os.environ.pop('VERCEL', None)


if __name__ == '__main__':
    unittest.main()

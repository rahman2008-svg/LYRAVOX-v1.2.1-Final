"""Local server for the WHOLE application: serves public/ and the same /api/* handlers that run on Vercel, on one origin (one port).

    python -m lyravox_ai.devserver                 # http://localhost:8000  (real engine: needs `pip install -r requirements.txt`)
    LYRAVOX_ALLOW_FAKE=1 python -m lyravox_ai.devserver --fake      # TEST-ONLY placeholder engine (text "TEST LINE n")

Equivalent of `vercel dev` without needing the Vercel CLI."""
import argparse, os, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_handler(public_dir):
    from . import web

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=public_dir, **k)

        def log_message(self, *a):
            pass

        def _api(self):
            path = self.path.split('?')[0].rstrip('/')
            if path == '/api/health':
                web.serve(self, 'health'); return True
            if path == '/api/transcribe':
                web.serve(self, 'transcribe'); return True
            if path.startswith('/api'):
                web.serve(self, 'unknown'); return True
            return False

        def do_GET(self):
            if not self._api(): super().do_GET()

        def do_HEAD(self):
            if not self._api(): super().do_HEAD()

        def do_POST(self):
            if not self._api():
                self.send_error(405)

        def send_error(self, code, message=None, explain=None):
            if code == 404 and not self.path.startswith('/api'):            # the site's own 404 page, like the static host would
                p = os.path.join(public_dir, '404.html')
                if os.path.exists(p):
                    with open(p, 'rb') as fh:
                        data = fh.read()
                    self.send_response(404); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(data))); self.end_headers()
                    if self.command != 'HEAD': self.wfile.write(data)
                    return
            super().send_error(code, message, explain)
    return H


def serve(host='127.0.0.1', port=8000, public_dir=None):
    srv = ThreadingHTTPServer((host, port), make_handler(public_dir or os.path.join(ROOT, 'public')))
    srv.daemon_threads = True
    return srv


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1'); ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--public', default=os.path.join(ROOT, 'public'))
    ap.add_argument('--fake', action='store_true', help='TEST ONLY placeholder engine (needs LYRAVOX_ALLOW_FAKE=1)')
    ap.add_argument('--fake-delay', type=float, default=0.0); ap.add_argument('--fake-fail-first', type=int, default=0, help='TEST ONLY: first N chunk requests fail')
    a = ap.parse_args()
    if a.fake:
        os.environ['LYRAVOX_ENGINE'] = 'fake'
        os.environ['LYRAVOX_FAKE_DELAY'] = str(a.fake_delay); os.environ['LYRAVOX_FAKE_FAIL_FIRST'] = str(a.fake_fail_first)
    from .core import get_core
    c = get_core()
    print(f'LYRAVOX on http://{a.host}:{a.port}  engine={c.transcriber.name} available={c.transcriber.available()}', file=sys.stderr)
    serve(a.host, a.port, a.public).serve_forever()

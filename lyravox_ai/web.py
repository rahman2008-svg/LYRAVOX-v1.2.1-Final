"""HTTP glue shared by the Vercel functions (api/*.py) and the local dev server: it runs on a standard-library BaseHTTPRequestHandler, so the exact
same code is deployed and tested. Responses are JSON, never cached, and there is no CORS: the API is for the site's own pages (same origin)."""
import json, sys, traceback
from urllib.parse import parse_qs, urlparse
from .core import get_core
from .errors import ApiError

WAV_TYPES = ('audio/wav', 'audio/x-wav', 'audio/wave', 'audio/vnd.wave', 'application/octet-stream')


def _send(h, status, obj, extra=None):
    body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    h.send_response(status)
    h.send_header('Content-Type', 'application/json; charset=utf-8')
    h.send_header('Content-Length', str(len(body)))
    h.send_header('Cache-Control', 'no-store')
    h.send_header('X-Content-Type-Options', 'nosniff')
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _same_origin(h):
    """Refuse requests that a browser marks as coming from another site, so nobody can use this deployment's compute from their own page.
    Non-browser clients (curl, tests) send neither header and are allowed; this is not authentication."""
    site = h.headers.get('Sec-Fetch-Site')
    if site and site not in ('same-origin', 'none'):
        raise ApiError(403, 'cross_site_blocked', 'This API can only be used from the LYRAVOX site itself.')
    origin = h.headers.get('Origin')
    host = h.headers.get('X-Forwarded-Host') or h.headers.get('Host')
    if origin and host and urlparse(origin).netloc != host:
        raise ApiError(403, 'cross_site_blocked', 'This API can only be used from the LYRAVOX site itself.')


def _client(h):
    fwd = h.headers.get('X-Forwarded-For') or h.headers.get('X-Real-IP')
    return (fwd.split(',')[0].strip() if fwd else h.client_address[0]) or 'unknown'


def _read_body(h, limit):
    cl = h.headers.get('Content-Length')
    if cl is None or not cl.isdigit():
        raise ApiError(411, 'length_required', 'Content-Length is required.')
    n = int(cl)
    if n > limit:
        h.close_connection = True
        raise ApiError(413, 'payload_too_large', f'The request is {n} bytes; the limit is {limit} bytes (Vercel allows at most 4.5 MB per request). Send smaller pieces.')
    buf = bytearray()
    while len(buf) < n:
        part = h.rfile.read(min(65536, n - len(buf)))
        if not part:
            raise ApiError(400, 'incomplete_body', 'The request body ended early.')
        buf += part
    return bytes(buf)


def serve(h, route):
    try:
        _same_origin(h)
        core = get_core()
        if route == 'health':
            if h.command not in ('GET', 'HEAD'):
                raise ApiError(405, 'method_not_allowed', 'Use GET.', {'Allow': 'GET, HEAD'})
            return _send(h, 200, core.health())
        if route == 'transcribe':
            if h.command == 'GET':                           # diagnostics of the function that actually transcribes (own /tmp on Vercel); ?warmup=1 also prepares the model
                q = {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items()}
                if q.get('warmup') in ('1', 'true'):
                    return _send(h, 200, {'status': 'ready', 'diagnostics': core.warmup()})
                return _send(h, 200, {'status': 'ok', 'diagnostics': core.diagnostics()})
            if h.command != 'POST':
                raise ApiError(405, 'method_not_allowed', 'Use POST with a WAV body (GET returns diagnostics).', {'Allow': 'GET, POST'})
            ctype = (h.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if ctype not in WAV_TYPES:
                raise ApiError(415, 'unsupported_type', f"Content-Type must be audio/wav (got '{ctype or 'none'}').")
            body = _read_body(h, core.cfg.max_chunk_bytes)
            q = {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items()}
            return _send(h, 200, core.transcribe(body, q, _client(h)))
        raise ApiError(404, 'not_found', 'No such endpoint.')
    except ApiError as e:
        _send(h, e.status, e.body(), e.headers)
    except Exception as e:                                   # never leak internals; the traceback goes to the platform log
        traceback.print_exc(file=sys.stderr)
        _send(h, 500, {'error': {'code': 'internal_error', 'message': f'Unexpected server error ({type(e).__name__}).'}})

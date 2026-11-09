"""Vercel Function: /api/health  (plain standard-library handler; all logic lives in lyravox_ai/)."""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from lyravox_ai import web  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        web.serve(self, 'health')

    def do_HEAD(self):
        web.serve(self, 'health')

    def do_POST(self):
        web.serve(self, 'health')

    def log_message(self, *args):                      # the platform keeps its own request log
        pass

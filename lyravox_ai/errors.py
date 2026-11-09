class ApiError(Exception):
    """An error with an HTTP status and a machine-readable code, returned to clients as {"error": {"code", "message"}}."""
    def __init__(self, status, code, message, headers=None, extra=None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers, self.extra = status, code, message, (headers or {}), (extra or {})

    def body(self):
        e = {'code': self.code, 'message': self.message}
        e.update(self.extra)                                   # additive diagnostics (stage, reason, diagnostics); code/message are unchanged
        return {'error': e}


class Cancelled(Exception):
    """Raised inside the pipeline when the job was cancelled."""


class JobTimeout(Exception):
    """Raised inside the pipeline when the job ran past its deadline."""


class AudioError(Exception):
    """The audio could not be read / decoded."""


class ModelError(Exception):
    """The Whisper model could not be prepared or loaded. `stage` says where: resolve | disk_check | download | verify | load."""
    def __init__(self, stage, reason, message, diagnostics=None):
        super().__init__(message)
        self.stage, self.reason, self.message, self.diagnostics = stage, reason, message, diagnostics or {}

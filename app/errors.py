"""Domain exceptions mapped to HTTP status codes in main.py."""


class ConflictError(Exception):
    """409: same (source_id, revision) with different content, or revision rollback."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class RebuildFailure(Exception):
    """A device rebuild could not complete; stale intervals remain untouched."""

    def __init__(self, device_id: str, reason: str):
        self.device_id = device_id
        self.reason = reason
        super().__init__(f"rebuild failed for device {device_id}: {reason}")

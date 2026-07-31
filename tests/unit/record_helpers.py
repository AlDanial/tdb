"""Shared capture recorder for gesture-hook tests."""


class CaptureRecorder:
    def __init__(self):
        self.records: list[tuple[str, list]] = []
        self.active = True
        self.on_error = None

    def record(self, action, params):
        self.records.append((action, list(params)))

    def close(self):
        pass

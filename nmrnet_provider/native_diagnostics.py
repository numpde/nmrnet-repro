"""Bound native reader/generator messages at capture, retaining explicit omission."""

from tools.container_guard import require_container

require_container()

import io


class NativeDiagnostic(io.TextIOBase):
    """Capture the one-use worker's native log writes, including success warnings."""

    def __init__(self):
        self.content = ""
        self.truncated = False

    def write(self, text):
        remaining = 8192 - len(self.content)
        self.content += text[:remaining]
        self.truncated |= len(text) > remaining
        return len(text)

    def value(self):
        if self.truncated:
            return self.content + "\n[RDKit diagnostic truncated]\n"
        return self.content

    def add_failure_note(self, failure):
        if self.value():
            failure.add_note("RDKit messages captured before the failure:\n" + self.value())

"""Hold the real entry rename without replacing the store's ownership machinery."""

import os
import threading


class EntryWriteBarrier:
    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.original = os.replace
        self.calls = 0
        self.failure = None

    def replace(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.reached.set()
            if not self.release.wait(3):
                raise TimeoutError("Test entry persistence was not released")
            if self.failure is not None:
                raise self.failure
        return self.original(*args, **kwargs)

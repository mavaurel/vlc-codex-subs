"""One concurrency and cooldown budget shared by a translation's CLI calls."""
from contextlib import contextmanager
from threading import Condition
import time

from .process import Cancelled


class RequestGate:
    def __init__(self, limit=1, on_wait=lambda *args: None, on_resume=lambda *args: None):
        self.limit = limit
        self.active = 0
        self.until = 0
        self.cooling = False
        self.condition = Condition()
        self.on_wait, self.on_resume = on_wait, on_resume

    def penalize(self, seconds):
        with self.condition:
            self.limit = max(1, self.limit//2)
            self.until = max(self.until, time.monotonic()+max(0, seconds))
            self.cooling = True
            delay, limit = self.until-time.monotonic(), self.limit
            self.condition.notify_all()
        self.on_wait(delay, limit)

    @contextmanager
    def slot(self, cancel=None):
        resumed = False
        with self.condition:
            while True:
                if cancel and cancel.exists(): raise Cancelled('Cancelled while waiting for Codex.')
                delay = self.until-time.monotonic()
                if self.active < self.limit and delay <= 0:
                    self.active += 1
                    if self.cooling:
                        self.cooling = False
                        resumed = True
                    break
                self.condition.wait(timeout=min(.25, delay) if delay > 0 else .25)
        try:
            if resumed: self.on_resume(self.limit)
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

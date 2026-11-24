"""A progressing scan has room to finish, but its child cannot run forever."""

import subprocess
from datetime import date

import pytest

from based_inventory.pipeline import extract


@pytest.mark.parametrize("finish_at", [11000, None])
def test_extractor_allows_slow_progress_but_stops_after_four_hours(monkeypatch, finish_at):
    clock = {"seconds": 0}

    class Child:
        args = ("synthetic extractor",)
        done = False
        terminated = False

        def wait(self, timeout):
            if self.terminated:
                self.done = True
                return -15
            clock["seconds"] += timeout
            if finish_at is not None and clock["seconds"] >= finish_at:
                self.done = True
                return 0
            raise subprocess.TimeoutExpired(self.args, timeout)

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.terminated = True

    child = Child()
    monkeypatch.setenv("RENDER_GIT_COMMIT", "synthetic-release")
    monkeypatch.setattr(extract.subprocess, "Popen", lambda *_a, **_kw: child)
    monkeypatch.setattr(extract.time, "monotonic", lambda: clock["seconds"])
    verified = object()
    monkeypatch.setattr(extract, "load_extract", lambda *_a: verified)
    if finish_at is None:
        with pytest.raises(TimeoutError):
            extract.extract_day(date(2026, 11, 23))
        assert child.terminated
        assert clock["seconds"] == 14430
    else:
        assert extract.extract_day(date(2026, 11, 23)) is verified
        assert not child.terminated

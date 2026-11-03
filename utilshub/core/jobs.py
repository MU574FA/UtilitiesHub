"""Run slow work off the Tk thread and get the answer back on it.

    app.jobs.submit(work, title="Reading CV", tool="cv", on_done=show, on_error=fail)

    def work(job):                       # runs in a worker thread
        for i, item in enumerate(items):
            if job.cancelled: return
            job.progress(i + 1, len(items), item.name)
        return result

Callbacks (on_done / on_error / on_progress) always run on the Tk thread.
`UiBridge` is the one place where threads talk to Tk: they put callables on a queue, Tk polls it.
"""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)
_ids = itertools.count(1)


class UiBridge:
    """Thread-safe 'call this on the UI thread' queue, polled by Tk's after()."""

    def __init__(self, root, interval_ms: int = 40) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.interval = interval_ms
        self._poll()

    def call(self, fn: Callable, *args, **kwargs) -> None:
        self.q.put((fn, args, kwargs))

    def _poll(self) -> None:
        try:
            for _ in range(200):                       # cap per tick so the UI never freezes
                fn, args, kwargs = self.q.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception:
                    log.exception("UI callback failed")
        except queue.Empty:
            pass
        try:
            self.root.after(self.interval, self._poll)
        except Exception:
            pass                                       # window already destroyed


@dataclass
class Job:
    id: int
    title: str
    tool: str = ""
    status: str = "queued"          # queued | running | done | error | cancelled
    done: int = 0
    total: int = 0
    message: str = ""
    error: str = ""
    result: Any = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _bridge: UiBridge | None = field(default=None, repr=False)
    _runner: "JobRunner | None" = field(default=None, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def progress(self, done: int, total: int = 0, message: str = "") -> None:
        self.done, self.total = done, total or self.total
        if message:
            self.message = message
        if self._runner:
            self._runner._changed(self)

    @property
    def fraction(self) -> float:
        return (self.done / self.total) if self.total else 0.0


class JobRunner:
    def __init__(self, bridge: UiBridge, max_workers: int = 3) -> None:
        self.bridge = bridge
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self.jobs: dict[int, Job] = {}
        self.listeners: list[Callable[[Job], None]] = []
        self._serial_locks: dict[str, threading.Lock] = {}

    def on_change(self, callback: Callable[[Job], None]) -> None:
        self.listeners.append(callback)

    def _changed(self, job: Job) -> None:
        for cb in list(self.listeners):
            self.bridge.call(cb, job)

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status in ("queued", "running")]

    def cancel_all(self) -> None:
        for j in self.active():
            j.cancel()

    def submit(self, fn: Callable[[Job], Any], title: str = "Working", tool: str = "",
               on_done: Callable[[Any], None] | None = None, on_error: Callable[[str], None] | None = None,
               on_progress: Callable[[Job], None] | None = None, serial_key: str = "") -> Job:
        """Queue `fn(job)`.  Jobs with the same `serial_key` run one at a time (e.g. OCR of one tool)."""
        job = Job(next(_ids), title, tool, _bridge=self.bridge, _runner=self)
        self.jobs[job.id] = job
        lock = self._serial_locks.setdefault(serial_key, threading.Lock()) if serial_key else None

        def run() -> None:
            if lock:
                lock.acquire()
            try:
                if job.cancelled:
                    job.status = "cancelled"
                    return
                job.status = "running"
                self._changed(job)
                result = fn(job)
                job.result = result
                job.status = "cancelled" if job.cancelled else "done"
                if on_done and job.status == "done":
                    self.bridge.call(on_done, result)
            except Exception as exc:
                job.status, job.error = "error", str(exc) or exc.__class__.__name__
                log.error("Job %r failed:\n%s", title, traceback.format_exc())
                if on_error:
                    self.bridge.call(on_error, job.error)
            finally:
                if lock:
                    lock.release()
                self._changed(job)
                self.bridge.call(self._cleanup, job.id)

        if on_progress:
            self.listeners.append(lambda j, cb=on_progress, mine=job.id: cb(j) if j.id == mine else None)
        self._changed(job)
        self.pool.submit(run)
        return job

    def _cleanup(self, job_id: int) -> None:
        job = self.jobs.get(job_id)
        if job and job.status not in ("queued", "running"):
            threading.Timer(30, lambda: self.jobs.pop(job_id, None)).start()

    def shutdown(self) -> None:
        self.cancel_all()
        self.pool.shutdown(wait=False, cancel_futures=True)

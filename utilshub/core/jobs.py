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

import collections
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
        self._lock = threading.Lock()
        self._serial_busy: set[str] = set()
        self._serial_waiting: dict[str, collections.deque] = {}
        self._progress_cbs: dict[int, Callable[[Job], None]] = {}

    def resize(self, workers: int) -> None:
        """Change how many jobs run at once (takes effect for new work)."""
        try:
            self.pool._max_workers = max(1, int(workers))          # ThreadPoolExecutor starts threads lazily up to this
        except Exception:
            log.debug("could not resize the job pool", exc_info=True)

    def on_change(self, callback: Callable[[Job], None]) -> None:
        self.listeners.append(callback)

    def _changed(self, job: Job) -> None:
        for cb in list(self.listeners):
            self.bridge.call(cb, job)
        mine = self._progress_cbs.get(job.id)
        if mine is not None:
            self.bridge.call(mine, job)

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
        if on_progress:
            self._progress_cbs[job.id] = on_progress

        def run() -> None:
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
                self._changed(job)
                self.bridge.call(self._cleanup, job.id)
                if serial_key:
                    self._next_serial(serial_key)

        self._changed(job)
        if serial_key:
            # one job per key at a time WITHOUT parking a worker thread on a lock: the rest wait in a queue
            with self._lock:
                if serial_key in self._serial_busy:
                    self._serial_waiting.setdefault(serial_key, collections.deque()).append(run)
                    return job
                self._serial_busy.add(serial_key)
        self.pool.submit(run)
        return job

    def _next_serial(self, key: str) -> None:
        with self._lock:
            queue = self._serial_waiting.get(key)
            if queue:
                nxt = queue.popleft()
            else:
                self._serial_busy.discard(key)
                self._serial_waiting.pop(key, None)
                return
        try:
            self.pool.submit(nxt)
        except RuntimeError:                                  # pool already shut down
            pass

    def _cleanup(self, job_id: int) -> None:
        """UI thread: forget a finished job after 30 s (no thread timers, so quitting is never delayed)."""
        job = self.jobs.get(job_id)
        if job and job.status not in ("queued", "running"):
            def drop() -> None:
                self.jobs.pop(job_id, None)
                self._progress_cbs.pop(job_id, None)
            try:
                self.bridge.root.after(30_000, drop)
            except Exception:
                drop()

    def shutdown(self) -> None:
        self.cancel_all()
        with self._lock:
            self._serial_waiting.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)

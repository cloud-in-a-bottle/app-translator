"""In-memory record of translation jobs.

Translating means talking to the network, which can be slow, so the request that
starts it returns immediately and the browser follows a job URL instead. Jobs live
in this process only — a restart loses them, which is fine for work that takes
seconds and is cheap to redo.
"""

import threading
import time
import uuid
from typing import Any
from typing import Literal

import attr

JobState = Literal["queued", "running", "done", "failed"]

# Keep the store small: jobs are only interesting while a browser is watching one.
MAX_JOB_AGE_SECONDS = 3600.0
MAX_JOBS = 200


@attr.s(auto_attribs=True, frozen=True)
class Job:
    job_id: str
    state: JobState
    source_label: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    # The review-page context, once the translation has succeeded.
    context: dict[str, Any] | None = None
    error_message: str = ""
    error_detail: str = ""

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.created_at

    @property
    def is_finished(self) -> bool:
        return self.state in ("done", "failed")


class JobStore:
    """Thread-safe because jobs are updated from worker threads."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, source_label: str) -> Job:
        job = Job(
            job_id=uuid.uuid4().hex[:12],
            state="queued",
            source_label=source_label,
            created_at=time.monotonic(),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._prune_locked()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes: Any) -> Job:  # noqa: ANN401 - attrs field values
        with self._lock:
            existing = self._jobs[job_id]
            updated = attr.evolve(existing, **changes)
            self._jobs[job_id] = updated
            return updated

    def mark_running(self, job_id: str) -> Job:
        return self.update(job_id, state="running", started_at=time.monotonic())

    def mark_done(self, job_id: str, context: dict[str, Any]) -> Job:
        return self.update(job_id, state="done", context=context, finished_at=time.monotonic())

    def mark_failed(self, job_id: str, message: str, detail: str = "") -> Job:
        return self.update(
            job_id,
            state="failed",
            error_message=message,
            error_detail=detail,
            finished_at=time.monotonic(),
        )

    def queue_length(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.state in ("queued", "running"))

    def _prune_locked(self) -> None:
        now = time.monotonic()
        stale = [
            job_id
            for job_id, job in self._jobs.items()
            if job.is_finished and now - job.created_at > MAX_JOB_AGE_SECONDS
        ]
        for job_id in stale:
            del self._jobs[job_id]
        if len(self._jobs) > MAX_JOBS:
            oldest = sorted(self._jobs.values(), key=lambda job: job.created_at)
            for job in oldest[: len(self._jobs) - MAX_JOBS]:
                if job.is_finished:
                    del self._jobs[job.job_id]

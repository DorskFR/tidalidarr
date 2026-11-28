import json
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, computed_field

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(UTC)


class GrabStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in (GrabStatus.QUEUED, GrabStatus.DOWNLOADING)


class GrabJob(BaseModel):
    id: str
    album_id: int
    title: str
    artist: str
    status: GrabStatus = GrabStatus.QUEUED
    tracks_done: int = 0
    tracks_total: int = 0
    attempts: int = 0
    error: str | None = None
    output_path: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress(self) -> float:
        if self.status is GrabStatus.COMPLETED:
            return 1.0
        return self.tracks_done / self.tracks_total if self.tracks_total else 0.0


class GrabStore:
    """Grab jobs by id, written to `path` after every change so they survive restarts."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._jobs: dict[str, GrabJob] = {}
        self._load()

    def get(self, job_id: str) -> GrabJob | None:
        return self._jobs.get(job_id)

    def pending(self) -> list[GrabJob]:
        return [job for job in self._jobs.values() if job.status.is_active]

    def put(self, job: GrabJob) -> GrabJob:
        self._jobs[job.id] = job
        self._save()
        return job

    def update(self, job_id: str, **fields: object) -> GrabJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        updated = job.model_copy(update=fields | {"updated_at": _now()})
        return self.put(updated)

    def remove(self, job_id: str) -> None:
        if self._jobs.pop(job_id, None) is not None:
            self._save()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            jobs = [GrabJob(**item) for item in raw]
        except (json.JSONDecodeError, ValidationError, TypeError):
            logger.exception(f"Ignoring unreadable grab store {self._path}")
            return
        cutoff = _now() - RETENTION
        self._jobs = {job.id: job for job in jobs if job.status.is_active or job.updated_at >= cutoff}
        logger.info(f"💽 Loaded {len(self._jobs)} grab jobs from {self._path}")

    def _save(self) -> None:
        if not self._path:
            return
        payload = [job.model_dump(mode="json", exclude={"progress"}) for job in self._jobs.values()]
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._path)

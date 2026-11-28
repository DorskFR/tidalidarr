from datetime import UTC, datetime, timedelta
from pathlib import Path

from tidalidarr.grabs import GrabJob, GrabStatus, GrabStore


def _job(job_id: str = "1", **fields: object) -> GrabJob:
    return GrabJob(id=job_id, album_id=int(job_id), title="Album", artist="Artist", **fields)  # type: ignore[arg-type]


def test_jobs_survive_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "grabs.json"
    store = GrabStore(path)
    store.put(_job("1", tracks_total=10))
    store.update("1", status=GrabStatus.DOWNLOADING, tracks_done=4)

    reloaded = GrabStore(path).get("1")

    assert reloaded is not None
    assert reloaded.status is GrabStatus.DOWNLOADING
    assert reloaded.tracks_done == 4
    assert reloaded.progress == 0.4
    assert [job.id for job in GrabStore(path).pending()] == ["1"]


def test_old_finished_jobs_are_pruned_but_active_ones_kept(tmp_path: Path) -> None:
    path = tmp_path / "grabs.json"
    old = datetime.now(UTC) - timedelta(days=60)
    store = GrabStore(path)
    store.put(_job("1", status=GrabStatus.COMPLETED, updated_at=old))
    store.put(_job("2", status=GrabStatus.QUEUED, updated_at=old))
    store.put(_job("3", status=GrabStatus.FAILED))

    reloaded = GrabStore(path)

    assert reloaded.get("1") is None
    assert reloaded.get("2") is not None
    assert reloaded.get("3") is not None


def test_an_unreadable_store_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "grabs.json"
    path.write_text("{not json", encoding="utf-8")

    assert GrabStore(path).pending() == []


def test_completed_progress_is_full_and_remove_forgets() -> None:
    store = GrabStore()
    store.put(_job("1", status=GrabStatus.COMPLETED))

    job = store.get("1")
    assert job is not None
    assert job.progress == 1.0
    store.remove("1")
    assert store.get("1") is None

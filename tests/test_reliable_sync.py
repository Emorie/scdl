import asyncio
import sys
import time
from pathlib import Path

import pytest

from scdl_web.reliable import ReliableConfig, ReliableSync, Store, classify_error, rate_limit_delay, sanitize_error


def test_track_identity_is_deduplicated_by_soundcloud_id(tmp_path: Path) -> None:
    store = Store(tmp_path / "app.db"); store.init()
    track = {"id": 99, "permalink_url": "https://soundcloud.com/a/b", "title": "same title"}
    assert store.insert_tracks([track, track]) == 1
    assert store.counts() == {"pending": 1}


def test_completed_missing_file_is_repair_needed(tmp_path: Path) -> None:
    store = Store(tmp_path / "app.db"); store.init()
    store.insert_tracks([{ "id": 1, "permalink_url": "https://soundcloud.com/a/b" }])
    store.update("1", status="completed", final_path=str(tmp_path / "missing.mp3"))
    store.reconcile()
    assert store.counts() == {"repair_needed": 1}


def test_one_hundred_completed_tracks_are_verified_locally_without_subprocess(tmp_path: Path) -> None:
    store = Store(tmp_path / "app.db"); store.init()
    for number in range(1, 101):
        media = tmp_path / f"{number}.mp3"; media.write_bytes(b"media")
        store.insert_tracks([{ "id": number, "permalink_url": f"https://soundcloud.com/a/{number}" }])
        store.update(str(number), status="completed", final_path=str(media), file_size=media.stat().st_size)
    assert all(store.completed_local(str(number)) for number in range(1, 101))


def test_429_is_not_confused_with_plain_timeout() -> None:
    assert classify_error("HTTP 429 Too Many Requests") == ("http_429", 429, True)
    assert classify_error("read timeout") == ("read_timeout", None, True)
    assert rate_limit_delay("HTTP 429 Retry-After: 31", 1) == (33, "Retry-After")


def test_invalid_concurrency_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCDL_MAX_CONCURRENT_DOWNLOADS", "2")
    with pytest.raises(ValueError, match="exactly 1"):
        ReliableConfig.from_env()


def test_collection_page_timeout_preserves_cursor(tmp_path: Path) -> None:
    def slow_discovery(cursor: object) -> tuple[list[dict[str, object]], object]:
        time.sleep(0.05)
        return [], cursor
    cfg = ReliableConfig(collection_page_timeout_seconds=1, min_free_space_gb=0)
    sync = ReliableSync(tmp_path, tmp_path, cfg, discover=slow_discovery)
    sync.store.init(); sync.store.set_state("likes_cursor", 42)
    # A very small value is set directly to exercise the timeout path without
    # adding an invalid production configuration value.
    object.__setattr__(sync.cfg, "collection_page_timeout_seconds", 0.001)
    with pytest.raises(RuntimeError, match="collection page timeout"):
        asyncio.run(sync._discover_if_due())
    assert sync.store.state("likes_cursor") == 42


def test_batch_claims_no_more_than_configured_limit(tmp_path: Path) -> None:
    store = Store(tmp_path / "app.db"); store.init()
    store.insert_tracks({"id": number, "permalink_url": f"https://soundcloud.com/a/{number}"} for number in range(12))
    batch_id = store.start_batch(5)
    assert batch_id
    assert store.batch_room(batch_id, 5) == 0
    with store.connect() as conn:
        assert conn.execute("SELECT initial_size FROM reliable_batches WHERE batch_id=?", (batch_id,)).fetchone()[0] == 5


def test_error_redaction_removes_signed_url_and_token() -> None:
    value = sanitize_error("https://cdn.example/file?Policy=secret token=abc123")
    assert "secret" not in value and "abc123" not in value and "[redacted]" in value


def test_success_is_marked_only_after_media_exists(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"; downloads.mkdir()
    cfg = ReliableConfig(min_free_space_gb=0, min_track_delay_seconds=0, max_track_delay_seconds=0, hard_min_delay_seconds=0)
    def command(_: str, staging: Path) -> list[str]:
        return [sys.executable, "-c", "import pathlib; pathlib.Path(__import__('sys').argv[1]).write_bytes(b'media')", str(staging / "song.mp3")]
    sync = ReliableSync(tmp_path, downloads, cfg, command_for=command)
    sync.store.init(); sync.store.insert_tracks([{ "id": 1, "permalink_url": "https://soundcloud.com/a/b" }])
    asyncio.run(sync._process(sync.store.eligible()))
    assert sync.store.counts() == {"completed": 1}
    assert (downloads / "song.mp3").stat().st_size > 0

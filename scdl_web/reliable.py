"""Persistent, deliberately slow likes synchronisation service.

This module is intentionally independent of the web queue.  The old queue is
still useful for one-off URLs; this worker is for an unattended, resumable
likes backfill and incremental sync.  Network access is always through the
installed, supported ``soundcloud-v2`` client and ``scdl`` executable.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

STATUSES = {"pending", "resolving", "downloading", "processing", "completed", "retry_wait", "unavailable", "repair_needed", "permanently_failed"}
TRANSIENT_DELAYS = (60, 300, 900, 2700, 7200, 21600)
RATE_LIMIT_DELAYS = (3600, 7200, 14400, 28800, 86400)


@dataclass(frozen=True)
class ReliableConfig:
    enabled: bool = False
    max_concurrent_downloads: int = 1
    batch_size: int = 500
    min_track_delay_seconds: int = 150
    max_track_delay_seconds: int = 240
    hard_min_delay_seconds: int = 120
    target_batch_min_hours: int = 24
    target_batch_max_hours: int = 36
    metadata_timeout_seconds: int = 60
    media_resolve_timeout_seconds: int = 90
    connect_timeout_seconds: int = 30
    read_timeout_seconds: int = 300
    ffmpeg_timeout_seconds: int = 1800
    likes_check_interval_minutes: int = 30
    min_free_space_gb: int = 20
    smoke_test_batch_size: int = 0
    diagnostic_mode: bool = False
    diagnostic_log_max_mb: int = 100
    diagnostic_log_backup_count: int = 10

    @classmethod
    def from_env(cls) -> "ReliableConfig":
        def integer(name: str, default: int) -> int:
            value = os.environ.get(name, str(default))
            try: return int(value)
            except ValueError: raise ValueError(f"{name} must be an integer")
        def truth(name: str, default: bool = False) -> bool:
            return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}
        cfg = cls(
            enabled=truth("SCDL_RELIABLE_SYNC_ENABLED"), max_concurrent_downloads=integer("SCDL_MAX_CONCURRENT_DOWNLOADS", 1),
            batch_size=integer("SCDL_BATCH_SIZE", 500), min_track_delay_seconds=integer("SCDL_MIN_TRACK_DELAY_SECONDS", 150),
            max_track_delay_seconds=integer("SCDL_MAX_TRACK_DELAY_SECONDS", 240), hard_min_delay_seconds=integer("SCDL_HARD_MIN_DELAY_SECONDS", 120),
            target_batch_min_hours=integer("SCDL_TARGET_BATCH_MIN_HOURS", 24), target_batch_max_hours=integer("SCDL_TARGET_BATCH_MAX_HOURS", 36),
            metadata_timeout_seconds=integer("SCDL_METADATA_TIMEOUT_SECONDS", 60), media_resolve_timeout_seconds=integer("SCDL_MEDIA_RESOLVE_TIMEOUT_SECONDS", 90),
            connect_timeout_seconds=integer("SCDL_CONNECT_TIMEOUT_SECONDS", 30), read_timeout_seconds=integer("SCDL_READ_TIMEOUT_SECONDS", 300), ffmpeg_timeout_seconds=integer("SCDL_FFMPEG_TIMEOUT_SECONDS", 1800),
            likes_check_interval_minutes=integer("SCDL_LIKES_CHECK_INTERVAL_MINUTES", 30), min_free_space_gb=integer("SCDL_MIN_FREE_SPACE_GB", 20),
            smoke_test_batch_size=integer("SCDL_SMOKE_TEST_BATCH_SIZE", 0), diagnostic_mode=truth("SCDL_DIAGNOSTIC_MODE"),
            diagnostic_log_max_mb=integer("SCDL_DIAGNOSTIC_LOG_MAX_MB", 100), diagnostic_log_backup_count=integer("SCDL_DIAGNOSTIC_LOG_BACKUP_COUNT", 10),
        )
        if cfg.max_concurrent_downloads != 1: raise ValueError("SCDL_MAX_CONCURRENT_DOWNLOADS must be exactly 1 for reliable sync")
        if cfg.batch_size < 1 or cfg.min_track_delay_seconds < 0 or cfg.max_track_delay_seconds < cfg.min_track_delay_seconds: raise ValueError("invalid reliable-sync batch or delay settings")
        if cfg.hard_min_delay_seconds > cfg.min_track_delay_seconds: raise ValueError("SCDL_HARD_MIN_DELAY_SECONDS cannot exceed normal minimum")
        if cfg.likes_check_interval_minutes < 5 or cfg.min_free_space_gb < 0 or cfg.diagnostic_log_max_mb < 1 or cfg.diagnostic_log_backup_count < 1: raise ValueError("invalid reliable-sync safety setting")
        if any(value <= 0 for value in (cfg.metadata_timeout_seconds, cfg.media_resolve_timeout_seconds, cfg.connect_timeout_seconds, cfg.read_timeout_seconds, cfg.ffmpeg_timeout_seconds)): raise ValueError("timeouts must be positive")
        return cfg


class Diagnostics:
    def __init__(self, directory: Path, cfg: ReliableConfig) -> None:
        self.path = directory / "diagnostics" / "scdl-events.jsonl"; self.cfg = cfg; self.path.parent.mkdir(parents=True, exist_ok=True)
    def emit(self, event: dict[str, Any]) -> None:
        if not self.cfg.diagnostic_mode: return
        event = {k: v for k, v in event.items() if k not in {"token", "authorization", "cookie", "url", "command", "response_body"}}
        event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        self._rotate();
        with self.path.open("a", encoding="utf-8") as out: out.write(json.dumps(event, sort_keys=True, default=str) + "\n")
    def _rotate(self) -> None:
        limit = self.cfg.diagnostic_log_max_mb * 1024 * 1024
        if not self.path.exists() or self.path.stat().st_size < limit: return
        for index in range(self.cfg.diagnostic_log_backup_count - 1, 0, -1):
            src, dst = self.path.with_suffix(f".jsonl.{index}"), self.path.with_suffix(f".jsonl.{index + 1}")
            if src.exists(): src.replace(dst)
        self.path.replace(self.path.with_suffix(".jsonl.1"))


class Store:
    def __init__(self, path: Path) -> None: self.path = path
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path); conn.row_factory = sqlite3.Row; conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA synchronous=NORMAL"); return conn
    def init(self) -> None:
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS reliable_tracks (
              track_id TEXT PRIMARY KEY, permalink TEXT NOT NULL, title TEXT, artist TEXT, discovered_at REAL NOT NULL, liked_at REAL,
              source_type TEXT NOT NULL DEFAULT 'likes', collection_name TEXT, status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
              last_attempt_at REAL, next_retry_at REAL, last_http_status INTEGER, last_failure_stage TEXT, error_summary TEXT,
              final_path TEXT, temporary_path TEXT, file_size INTEGER, completed_at REAL, verified_at REAL, batch_id TEXT);
            CREATE INDEX IF NOT EXISTS idx_reliable_ready ON reliable_tracks(status, next_retry_at, discovered_at);
            CREATE TABLE IF NOT EXISTS reliable_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reliable_batches (batch_id TEXT PRIMARY KEY, started_at REAL NOT NULL, initial_size INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0, unavailable INTEGER NOT NULL DEFAULT 0, retry_wait INTEGER NOT NULL DEFAULT 0, permanently_failed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS reliable_events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, track_id TEXT, batch_id TEXT, stage TEXT, http_status INTEGER, error_class TEXT, duration_seconds REAL, payload_json TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS idx_reliable_events_created ON reliable_events(created_at);
            """)
    def state(self, key: str, default: Any = None) -> Any:
        with self.connect() as c:
            row = c.execute("SELECT value FROM reliable_state WHERE key=?", (key,)).fetchone(); return json.loads(row[0]) if row else default
    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as c: c.execute("INSERT INTO reliable_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
    def start_batch(self, size: int) -> str | None:
        current = self.state("active_batch_id")
        if current:
            return str(current)
        batch_id = uuid.uuid4().hex
        with self.connect() as c:
            c.execute("INSERT INTO reliable_batches(batch_id,started_at,initial_size) VALUES(?,?,0)", (batch_id, time.time()))
            c.execute("UPDATE reliable_tracks SET batch_id=? WHERE track_id IN (SELECT track_id FROM reliable_tracks WHERE batch_id IS NULL AND status IN ('pending','repair_needed') ORDER BY discovered_at LIMIT ?)", (batch_id, size))
            claimed = c.execute("SELECT COUNT(*) FROM reliable_tracks WHERE batch_id=?", (batch_id,)).fetchone()[0]
            c.execute("UPDATE reliable_batches SET initial_size=? WHERE batch_id=?", (claimed, batch_id))
            if not claimed:
                c.execute("DELETE FROM reliable_batches WHERE batch_id=?", (batch_id,))
                return None
        self.set_state("active_batch_id", batch_id)
        self.set_state("active_batch_limit", size)
        return batch_id
    def batch_room(self, batch_id: str, limit: int) -> int:
        with self.connect() as c:
            used = c.execute("SELECT COUNT(*) FROM reliable_tracks WHERE batch_id=?", (batch_id,)).fetchone()[0]
        return max(0, limit - int(used))
    def insert_tracks(self, tracks: Iterable[dict[str, Any]], *, batch_id: str | None = None, limit: int | None = None) -> int:
        now = time.time(); count = 0
        with self.connect() as c:
            for t in tracks:
                if limit is not None and count >= limit: break
                if not t.get("id") or not t.get("permalink_url"): continue
                before = c.total_changes
                c.execute("INSERT OR IGNORE INTO reliable_tracks(track_id,permalink,title,artist,discovered_at,liked_at,source_type,collection_name,batch_id) VALUES(?,?,?,?,?,?,?,?,?)", (str(t["id"]), t["permalink_url"], t.get("title"), t.get("artist"), now, t.get("liked_at"), t.get("source_type", "likes"), t.get("collection_name"), batch_id))
                count += c.total_changes - before
            if batch_id and count:
                c.execute("UPDATE reliable_batches SET initial_size=(SELECT COUNT(*) FROM reliable_tracks WHERE batch_id=?) WHERE batch_id=?", (batch_id, batch_id))
        return count
    def eligible(self) -> sqlite3.Row | None:
        active = self.state("active_batch_id")
        with self.connect() as c:
            if active:
                return c.execute("SELECT * FROM reliable_tracks WHERE batch_id=? AND status IN ('pending','retry_wait','repair_needed') AND COALESCE(next_retry_at,0)<=? ORDER BY discovered_at LIMIT 1", (active, time.time())).fetchone()
            return c.execute("SELECT * FROM reliable_tracks WHERE status IN ('pending','retry_wait','repair_needed') AND COALESCE(next_retry_at,0)<=? ORDER BY discovered_at LIMIT 1", (time.time(),)).fetchone()
    def update(self, track_id: str, **fields: Any) -> None:
        if not fields: return
        fields["last_attempt_at"] = fields.get("last_attempt_at", time.time())
        cols = ", ".join(f"{k}=?" for k in fields); values = list(fields.values()) + [track_id]
        with self.connect() as c: c.execute(f"UPDATE reliable_tracks SET {cols} WHERE track_id=?", values)
    def counts(self) -> dict[str, int]:
        with self.connect() as c: return {r[0]: r[1] for r in c.execute("SELECT status,COUNT(*) FROM reliable_tracks GROUP BY status")}
    def refresh_batch(self, batch_id: str | None) -> bool:
        if not batch_id: return False
        with self.connect() as c:
            counts = {r[0]: r[1] for r in c.execute("SELECT status,COUNT(*) FROM reliable_tracks WHERE batch_id=? GROUP BY status", (batch_id,))}
            c.execute("UPDATE reliable_batches SET completed=?, unavailable=?, retry_wait=?, permanently_failed=? WHERE batch_id=?", (counts.get("completed", 0), counts.get("unavailable", 0), counts.get("retry_wait", 0), counts.get("permanently_failed", 0), batch_id))
        unfinished = sum(counts.get(s, 0) for s in ("pending", "resolving", "downloading", "processing", "repair_needed"))
        if not unfinished:
            self.set_state("active_batch_id", None)
            return True
        return False
    def reconcile(self) -> None:
        with self.connect() as c:
            rows = c.execute("SELECT track_id,status,final_path FROM reliable_tracks WHERE status IN ('resolving','downloading','processing','completed')").fetchall()
            for r in rows:
                final = Path(r["final_path"]) if r["final_path"] else None
                if r["status"] == "completed" and final and final.exists() and final.stat().st_size > 0: continue
                status = "repair_needed" if r["status"] == "completed" else "retry_wait"
                c.execute("UPDATE reliable_tracks SET status=?, next_retry_at=? WHERE track_id=?", (status, time.time(), r["track_id"]))
    def record_event(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        with self.connect() as c:
            c.execute("INSERT INTO reliable_events(created_at,track_id,batch_id,stage,http_status,error_class,duration_seconds,payload_json) VALUES(?,?,?,?,?,?,?,?)", (time.time(), payload.pop("track_id", None), payload.pop("batch_id", None), payload.pop("stage", None), payload.pop("http_status", None), payload.pop("error_class", None), payload.pop("duration_seconds", None), json.dumps(payload, default=str)))
    def failure_summary(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute("SELECT http_status,stage,error_class,strftime('%H', created_at, 'unixepoch') AS hour_utc,COUNT(*) AS count FROM reliable_events WHERE error_class IS NOT NULL OR http_status IS NOT NULL GROUP BY http_status,stage,error_class,hour_utc ORDER BY count DESC").fetchall()
        return [dict(row) for row in rows]


def classify_error(exc: BaseException | str) -> tuple[str, int | None, bool]:
    text = str(exc); lower = text.lower(); status = next((int(x) for x in text.replace("=", " ").split() if x.isdigit() and len(x) == 3), None)
    if status == 429: return "http_429", status, True
    if status == 401: return "authentication", status, False
    if status == 403: return "access_forbidden", status, False
    if status in (404, 410) or "private" in lower or "region" in lower: return "unavailable", status, False
    if status and status >= 500: return "remote_5xx", status, True
    if "timeout" in lower or "read timed out" in lower: return "read_timeout", status, True
    if "dns" in lower or "name resolution" in lower: return "dns", status, True
    if "ffmpeg" in lower: return "ffmpeg", status, False
    if "tag" in lower or "mutagen" in lower: return "tagging", status, False
    if "permission" in lower or "no space" in lower or "i/o" in lower or "read-only file" in lower: return "local_io", status, False
    return "unknown", status, True


def sanitize_error(value: BaseException | str) -> str:
    """Keep failure evidence useful without persisting credentials or signed URLs."""
    text = str(value)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", text)
    text = re.sub(r"(?i)(authorization|cookie|token|client_secret)\s*[:=]\s*[^\s,]+", r"\1=[redacted]", text)
    return text[:500]


def rate_limit_delay(error: BaseException | str, attempts: int) -> tuple[float, str]:
    """Use a server wait verbatim when scdl surfaces it; otherwise persist the
    documented conservative fallback sequence.  A tiny margin is only added
    after an explicit server wait, never used to shorten it."""
    text = str(error)
    match = re.search(r"retry[- ]after\s*[:=]?\s*(\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1)) + 2, "Retry-After"
    reset = re.search(r"(?:x-)?rate[- ]limit[- ]reset\s*[:=]?\s*(\d{10})", text, re.IGNORECASE)
    if reset:
        return max(0, int(reset.group(1)) - time.time()) + 2, "rate-limit reset"
    return RATE_LIMIT_DELAYS[min(attempts - 1, len(RATE_LIMIT_DELAYS) - 1)], "fallback"


class ReliableSync:
    """Single-worker scheduler. ``discover`` and ``command_for`` are injected for testability."""
    def __init__(self, config_dir: Path, download_dir: Path, cfg: ReliableConfig, discover: Callable[[Any], tuple[list[dict[str, Any]], Any]] | None = None, command_for: Callable[[str, Path], list[str] | tuple[list[str], Path]] | None = None) -> None:
        self.cfg, self.store, self.download_dir = cfg, Store(config_dir / "app.db"), download_dir
        self.diag, self.discover, self.command_for = Diagnostics(config_dir, cfg), discover, command_for
        self.task: asyncio.Task | None = None; self.stop_requested = asyncio.Event(); self.current: str | None = None; self.last_success: float | None = None
        self.process: asyncio.subprocess.Process | None = None
    async def start(self) -> None:
        self.store.init(); self.store.reconcile()
        if self.cfg.enabled and not self.task: self.task = asyncio.create_task(self.run(), name="reliable-scdl-sync")
    def _emit(self, event: dict[str, Any]) -> None:
        event = {**event, "error": sanitize_error(event["error"]) if event.get("error") else None}
        self.store.record_event(event)
        self.diag.emit(event)
    async def stop(self) -> None:
        self.stop_requested.set()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(), timeout=20)
            except asyncio.TimeoutError: self.process.kill(); await self.process.wait()
        if self.task: self.task.cancel()
        if self.task:
            try: await self.task
            except asyncio.CancelledError: pass
    def health(self) -> dict[str, Any]:
        cooldown = self.store.state("global_cooldown_until")
        free = shutil.disk_usage(self.download_dir).free if self.download_dir.exists() else 0
        return {"enabled": self.cfg.enabled, "running": bool(self.task and not self.task.done()), "current_track": self.current, "queue": self.store.counts(), "active_cooldown_until": cooldown, "authentication_paused": bool(self.store.state("authentication_paused", False)), "manually_paused": bool(self.store.state("manually_paused", False)), "last_success": self.last_success, "disk_free_bytes": free, "diagnostic_mode": self.cfg.diagnostic_mode, "config": asdict(self.cfg)}
    async def run(self) -> None:
        while not self.stop_requested.is_set():
            try:
                if self.store.state("manually_paused", False) or self.store.state("authentication_paused", False): await asyncio.sleep(30); continue
                if self._low_space(): await asyncio.sleep(60); continue
                if (until := self.store.state("global_cooldown_until")) and until > time.time(): await asyncio.sleep(min(60, until - time.time())); continue
                if not self.store.eligible():
                    if not self.store.state("active_batch_id") and self.store.start_batch(self.cfg.smoke_test_batch_size or self.cfg.batch_size):
                        continue
                    await self._discover_if_due(); await asyncio.sleep(5); continue
                await self._process(self.store.eligible())
                await asyncio.sleep(self._delay())
            except asyncio.CancelledError: raise
            except Exception as exc: self._emit({"stage": "scheduler", "error_class": classify_error(exc)[0], "error": sanitize_error(exc)}); await asyncio.sleep(60)
    def _low_space(self) -> bool:
        return not self.download_dir.exists() or shutil.disk_usage(self.download_dir).free < self.cfg.min_free_space_gb * 1024**3
    def _delay(self) -> float: return max(self.cfg.hard_min_delay_seconds, random.uniform(self.cfg.min_track_delay_seconds, self.cfg.max_track_delay_seconds))
    async def _discover_if_due(self) -> None:
        if not self.discover: return
        due = self.store.state("next_likes_check", 0)
        if due > time.time(): return
        cursor = self.store.state("likes_cursor")
        started = time.monotonic()
        tracks, cursor = await asyncio.to_thread(self.discover, cursor)
        effective_size = self.cfg.smoke_test_batch_size or self.cfg.batch_size
        inserted = self.store.insert_tracks(tracks)
        batch_id = self.store.start_batch(effective_size)
        self.store.set_state("likes_cursor", cursor); self.store.set_state("last_likes_check", time.time()); self.store.set_state("next_likes_check", time.time() + 60 * self.cfg.likes_check_interval_minutes + random.uniform(0, 60))
        self._emit({"stage": "likes_pagination", "batch_id": batch_id, "duration_seconds": time.monotonic()-started, "discovered": len(tracks), "inserted": inserted})
    async def _process(self, row: sqlite3.Row | None) -> None:
        if not row or not self.command_for: return
        self.current = row["track_id"]; started = time.monotonic(); self.store.update(self.current, status="resolving", attempt_count=int(row["attempt_count"])+1)
        staging = self.download_dir / ".scdl-staging" / self.current; staging.mkdir(parents=True, exist_ok=True)
        self.store.update(self.current, status="downloading", temporary_path=str(staging))
        try:
            built = self.command_for(row["permalink"], staging)
            command, final_dir = built if isinstance(built, tuple) else (built, self.download_dir)
            proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            self.process = proc
            try: output, _ = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.ffmpeg_timeout_seconds)
            except asyncio.TimeoutError: proc.terminate(); await proc.wait(); raise RuntimeError("subprocess timeout")
            if proc.returncode: raise RuntimeError(sanitize_error(output.decode("utf-8", "replace")[-1000:]))
            files = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() in {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"} and p.stat().st_size > 0]
            if not files: raise RuntimeError("scdl exited successfully without a media file")
            source = files[0]; final_dir.mkdir(parents=True, exist_ok=True); final = final_dir / source.name
            self.store.update(self.current, status="processing"); os.replace(source, final)
            self.store.update(self.current, status="completed", final_path=str(final), temporary_path=None, file_size=final.stat().st_size, completed_at=time.time(), verified_at=time.time(), last_failure_stage=None, error_summary=None, next_retry_at=None)
            self.last_success = time.time(); self._emit({"stage":"completed", "track_id":self.current, "duration_seconds":time.monotonic()-started, "file_size":final.stat().st_size})
        except Exception as exc:
            stage, status, retryable = classify_error(exc); attempts = int(row["attempt_count"])+1
            if stage == "http_429":
                delay, cooldown_source = rate_limit_delay(exc, attempts); until = time.time()+delay; self.store.set_state("global_cooldown_until", until)
            elif stage == "authentication":
                # A supported refresh is owned by the installed SoundCloud client.
                # Do not repeatedly manufacture credentials or retry a bad session.
                delay = 24 * 3600; self.store.set_state("authentication_paused", True)
            else: delay = TRANSIENT_DELAYS[min(attempts-1, len(TRANSIENT_DELAYS)-1)] if retryable else 7*86400
            next_status = "retry_wait" if retryable or stage == "authentication" else "unavailable"
            self.store.update(self.current, status=next_status, last_http_status=status, last_failure_stage=stage, error_summary=sanitize_error(exc), next_retry_at=time.time()+delay)
            self._emit({"stage":stage,"track_id":self.current,"http_status":status,"error_class":stage,"retry_attempt":attempts,"next_eligible_retry":time.time()+delay,"cooldown_source": cooldown_source if stage == "http_429" else None,"duration_seconds":time.monotonic()-started})
        finally:
            self.store.refresh_batch(row["batch_id"])
            self.process = None
            self.current = None

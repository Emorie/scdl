from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse, urlunparse

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scdl_web import APP_VERSION

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads")).resolve()
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config")).resolve()
LOG_DIR = CONFIG_DIR / "logs"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
ARCHIVE_PATH = CONFIG_DIR / "archive.txt"
DB_PATH = CONFIG_DIR / "app.db"
STATIC_DIR = Path(__file__).with_name("static")

TOKEN_MASK = "********"
MAX_LOG_LINES = 1200
RECENT_FILE_LIMIT = 100
ARCHIVE_IMPORT_LIMIT = 5 * 1024 * 1024
ACTIVE_STATUSES = {"Pending", "Running"}
RATE_LIMITED_STATUS = "Paused - Rate Limited"
TERMINAL_STATUSES = {"Done", "Skipped", "Cancelled"}
RETRYABLE_STATUSES = {"Failed", "Skipped", "Cancelled", RATE_LIMITED_STATUS}
AUDIO_EXTENSIONS = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
RELATED_FILE_EXTENSIONS = {".jpeg", ".jpg", ".png", ".txt"}
MEDIA_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".png",
    ".txt",
    ".wav",
}
ORGANIZATION_MODES = {
    "library-clean": "Library Clean",
    "flat": "Flat Downloads",
    "by-artist": "By Artist",
    "by-playlist": "By Playlist",
    "by-source-type": "By Source Type",
    "scdl-default": "Original Structure / scdl Default",
}
ARTIST_PRIORITY_MODES = {
    "smart-auto": "Smart Auto",
    "uploader-first": "Uploader First",
    "tagged-first": "Tagged Metadata First",
    "title-parse-first": "Title Parse First",
}
PROFILE_DOWNLOAD_TYPES = {
    "uploads": {"label": "Uploads only", "flag": "-t", "job_type": "Profile Uploads"},
    "all": {"label": "All tracks + reposts", "flag": "-a", "job_type": "Profile All Tracks + Reposts"},
    "likes": {"label": "Likes/favorites", "flag": "-f", "job_type": "Profile Likes"},
    "playlists": {"label": "Playlists", "flag": "-p", "job_type": "Profile Playlists"},
    "reposts": {"label": "Reposts", "flag": "-r", "job_type": "Profile Reposts"},
}
PROFILE_PATH_DEFAULTS = {
    "tracks": "uploads",
    "popular-tracks": "uploads",
    "albums": "playlists",
    "sets": "playlists",
    "reposts": "reposts",
    "likes": "likes",
}


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


DEFAULT_SETTINGS: dict[str, Any] = {
    "auth_token": "",
    "archive_enabled": True,
    "name_format": "",
    "playlist_name_format": "",
    "no_playlist_folder": False,
    "artist_folders": False,
    "original_art": False,
    "add_description": False,
    "artist_metadata_priority": "smart-auto",
    "preserve_original_metadata": True,
    "force_metadata": False,
    "save_sidecar_json": False,
    "embed_soundcloud_tags": True,
    "parse_artist_from_title": True,
    "search_tags_enabled": True,
    "organization_mode": "library-clean",
    "use_playlist_folders": True,
    "put_likes_in_likes_folder": True,
    "put_singles_in_singles_folder": False,
    "sanitize_filenames": True,
    "include_track_id_in_filename": False,
    "include_upload_date_in_filename": False,
    "max_concurrent_downloads": env_int("MAX_CONCURRENT_DOWNLOADS", 1),
    "download_delay_seconds": env_float("DOWNLOAD_DELAY_SECONDS", 2),
    "max_rate_limit_backoff_seconds": env_int("MAX_RATE_LIMIT_BACKOFF_SECONDS", 900),
    "max_consecutive_rate_limits": env_int("MAX_CONSECUTIVE_RATE_LIMITS", 8),
    "default_profile_download_type": os.environ.get("DEFAULT_PROFILE_DOWNLOAD_TYPE", "uploads"),
    "default_preset": os.environ.get("DEFAULT_PRESET", "best-original"),
}


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    args: tuple[str, ...]
    needs_url: bool = True
    downloads: bool = True


PRESETS: dict[str, Preset] = {
    "best-original": Preset(
        "best-original",
        "Best Original Available",
        "Tries original/lossless first, then falls back to the best available stream.",
        ("-l", "{url}", "--best-quality"),
    ),
    "original-only": Preset(
        "original-only",
        "Original Only",
        "Only downloads tracks where SoundCloud exposes the original file.",
        ("-l", "{url}", "--only-original"),
    ),
    "prefer-opus": Preset(
        "prefer-opus",
        "Prefer Opus",
        "Prefers Opus streams when original files are not used.",
        ("-l", "{url}", "--opus"),
    ),
    "no-original": Preset(
        "no-original",
        "Stream Only / No Original",
        "Skips original files and downloads the best allowed stream.",
        ("-l", "{url}", "--no-original"),
    ),
    "check-qualities": Preset(
        "check-qualities",
        "Check Qualities",
        "Lists SoundCloud stream qualities without downloading.",
        ("-l", "{url}", "--list-qualities"),
        downloads=False,
    ),
    "likes-best": Preset(
        "likes-best",
        "My Likes Best Quality",
        "Downloads your likes with best-quality mode. Requires a SoundCloud auth token.",
        ("me", "-f", "--best-quality"),
        needs_url=False,
    ),
    "playlist-best": Preset(
        "playlist-best",
        "Playlist Best Quality",
        "Downloads a playlist using best-quality mode.",
        ("-l", "{url}", "--best-quality"),
    ),
    "force-metadata": Preset(
        "force-metadata",
        "Metadata Repair / Force Metadata",
        "Repairs metadata for existing files where possible.",
        ("-l", "{url}", "--force-metadata"),
    ),
    "profile-uploads": Preset(
        "profile-uploads",
        "Profile Uploads / Tracks",
        "Downloads a SoundCloud profile's uploads/tracks with best-quality mode.",
        ("-l", "{url}", "--best-quality"),
    ),
}


class QueueAddRequest(BaseModel):
    urls: str | list[str] = ""
    preset: str = "best-original"
    autostart: bool = False
    archive_enabled: Optional[bool] = None
    profile_type: Optional[str] = None


class QualityRequest(BaseModel):
    url: str


class UrlInfoRequest(BaseModel):
    url: str


class SettingsUpdate(BaseModel):
    auth_token: Optional[str] = None
    clear_auth_token: bool = False
    archive_enabled: Optional[bool] = None
    name_format: Optional[str] = None
    playlist_name_format: Optional[str] = None
    no_playlist_folder: Optional[bool] = None
    artist_folders: Optional[bool] = None
    original_art: Optional[bool] = None
    add_description: Optional[bool] = None
    artist_metadata_priority: Optional[str] = None
    preserve_original_metadata: Optional[bool] = None
    force_metadata: Optional[bool] = None
    save_sidecar_json: Optional[bool] = None
    embed_soundcloud_tags: Optional[bool] = None
    parse_artist_from_title: Optional[bool] = None
    search_tags_enabled: Optional[bool] = None
    organization_mode: Optional[str] = None
    use_playlist_folders: Optional[bool] = None
    put_likes_in_likes_folder: Optional[bool] = None
    put_singles_in_singles_folder: Optional[bool] = None
    sanitize_filenames: Optional[bool] = None
    include_track_id_in_filename: Optional[bool] = None
    include_upload_date_in_filename: Optional[bool] = None
    max_concurrent_downloads: Optional[int] = None
    download_delay_seconds: Optional[float] = None
    max_rate_limit_backoff_seconds: Optional[int] = None
    max_consecutive_rate_limits: Optional[int] = None
    default_profile_download_type: Optional[str] = None
    default_preset: Optional[str] = None


class ConfirmRequest(BaseModel):
    confirm: bool = False


@dataclass
class QueueItem:
    id: str
    preset_id: str
    preset_name: str
    target: str
    target_url: str
    url_kind: str
    profile_type: Optional[str]
    job_type: str
    command: list[str]
    masked_command: list[str]
    log_path: Path
    archive_enabled: bool
    is_likes_sync: bool = False
    status: str = "Pending"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    return_code: Optional[int] = None
    output_file: Optional[str] = None
    track_id: Optional[str] = None
    last_error: Optional[str] = None
    rate_limit_count: int = 0
    last_rate_limit_backoff: Optional[int] = None
    rate_limit_retry_at: Optional[float] = None
    logs: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    metadata_records: list[dict[str, Any]] = field(default_factory=list)
    process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    rate_limit_pause_requested: bool = field(default=False, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "target": self.target,
            "target_url": self.target_url,
            "url_kind": self.url_kind,
            "profile_type": self.profile_type,
            "job_type": self.job_type,
            "command": self.masked_command,
            "archive_enabled": self.archive_enabled,
            "is_likes_sync": self.is_likes_sync,
            "status": self.status,
            "created_at": iso_time(self.created_at),
            "updated_at": iso_time(self.updated_at),
            "started_at": iso_time(self.started_at),
            "finished_at": iso_time(self.finished_at),
            "return_code": self.return_code,
            "output_file": self.output_file,
            "track_id": self.track_id,
            "last_error": self.last_error,
            "rate_limit_count": self.rate_limit_count,
            "last_rate_limit_backoff": self.last_rate_limit_backoff,
            "rate_limit_retry_at": iso_time(self.rate_limit_retry_at),
            "logs": self.logs[-200:],
            "log_path": str(self.log_path),
            "files": self.files,
            "summary": self.summary,
            "metadata_records": self.metadata_records,
        }


def iso_time(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_directories() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PATH.touch(exist_ok=True)


def db_connect() -> sqlite3.Connection:
    ensure_directories()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_queue_column(conn: sqlite3.Connection, name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(queue_items)").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE queue_items ADD COLUMN {name} {definition}")


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_items (
                id TEXT PRIMARY KEY,
                preset_id TEXT NOT NULL,
                preset_name TEXT NOT NULL,
                target TEXT NOT NULL,
                target_url TEXT NOT NULL DEFAULT '',
                url_kind TEXT NOT NULL DEFAULT 'unknown',
                profile_type TEXT,
                job_type TEXT NOT NULL DEFAULT 'Download',
                status TEXT NOT NULL,
                archive_enabled INTEGER NOT NULL DEFAULT 1,
                is_likes_sync INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                return_code INTEGER,
                log_path TEXT,
                files_json TEXT NOT NULL DEFAULT '[]',
                summary_json TEXT NOT NULL DEFAULT '{}',
                output_file TEXT,
                track_id TEXT,
                last_error TEXT,
                rate_limit_count INTEGER NOT NULL DEFAULT 0,
                last_rate_limit_backoff INTEGER,
                rate_limit_retry_at REAL
            )
            """,
        )
        ensure_queue_column(conn, "url_kind", "TEXT NOT NULL DEFAULT 'unknown'")
        ensure_queue_column(conn, "profile_type", "TEXT")
        ensure_queue_column(conn, "job_type", "TEXT NOT NULL DEFAULT 'Download'")
        ensure_queue_column(conn, "rate_limit_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_queue_column(conn, "last_rate_limit_backoff", "INTEGER")
        ensure_queue_column(conn, "rate_limit_retry_at", "REAL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_updated ON queue_items(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_likes ON queue_items(is_likes_sync, status)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata_records (
                id TEXT PRIMARY KEY,
                queue_item_id TEXT NOT NULL,
                output_path TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                uploader TEXT,
                tagged_artist TEXT,
                parsed_artist TEXT,
                genre TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                tags_text TEXT,
                description TEXT,
                source_url TEXT,
                track_id TEXT,
                playlist TEXT,
                album_or_playlist_title TEXT,
                artwork_url TEXT,
                created_or_release_date TEXT,
                downloaded_at REAL NOT NULL,
                quality_result_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL,
                UNIQUE(queue_item_id, output_path)
            )
            """,
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_queue ON metadata_records(queue_item_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_output ON metadata_records(output_path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_track ON metadata_records(track_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_search ON metadata_records(tags_text, artist, title)")
        conn.execute(
            """
            UPDATE queue_items
            SET status = 'Pending',
                updated_at = ?,
                last_error = COALESCE(last_error, 'Recovered after app restart; archive will skip completed tracks.')
            WHERE status = 'Running'
            """,
            (time.time(),),
        )
        conn.commit()


def json_loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def metadata_row_public(row: sqlite3.Row) -> dict[str, Any]:
    record = json_loads(row["metadata_json"], {})
    public = {
        "id": row["id"],
        "queue_item_id": row["queue_item_id"],
        "output_path": row["output_path"],
        "title": row["title"],
        "artist": row["artist"],
        "uploader": row["uploader"],
        "tagged_artist": row["tagged_artist"],
        "parsed_artist": row["parsed_artist"],
        "genre": row["genre"],
        "tags": json_loads(row["tags_json"], []),
        "description": row["description"],
        "source_url": row["source_url"],
        "track_id": row["track_id"],
        "playlist": row["playlist"],
        "album_or_playlist_title": row["album_or_playlist_title"],
        "artwork_url": row["artwork_url"],
        "created_or_release_date": row["created_or_release_date"],
        "downloaded_at": iso_time(row["downloaded_at"]),
        "quality_result": json_loads(row["quality_result_json"], {}),
    }
    public.update({key: value for key, value in record.items() if key not in public})
    return public


def metadata_for_queue(queue_item_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM metadata_records
            WHERE queue_item_id = ?
            ORDER BY downloaded_at DESC
            LIMIT ?
            """,
            (queue_item_id, limit),
        ).fetchall()
    return [metadata_row_public(row) for row in rows]


def metadata_by_output_path(output_path: str) -> Optional[dict[str, Any]]:
    try:
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM metadata_records
                WHERE output_path = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (output_path,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return metadata_row_public(row) if row else None


def upsert_metadata_record(queue_item_id: str, record: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    tags = normalize_tags(record.get("tags"))
    record = dict(record)
    record["tags"] = tags
    record.setdefault("downloaded_at", now)
    record.setdefault("updated_at", now)
    record_id = str(record.get("id") or uuid.uuid4().hex[:16])
    output_path = str(record.get("output_path") or "")
    tags_text = " ".join(
        str(part)
        for part in [
            record.get("title"),
            record.get("artist"),
            record.get("uploader"),
            record.get("tagged_artist"),
            record.get("parsed_artist"),
            record.get("genre"),
            record.get("playlist"),
            record.get("source_url"),
            record.get("track_id"),
            *tags,
        ]
        if part
    )
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO metadata_records (
                id, queue_item_id, output_path, title, artist, uploader, tagged_artist,
                parsed_artist, genre, tags_json, tags_text, description, source_url,
                track_id, playlist, album_or_playlist_title, artwork_url,
                created_or_release_date, downloaded_at, quality_result_json,
                metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(queue_item_id, output_path) DO UPDATE SET
                title = excluded.title,
                artist = excluded.artist,
                uploader = excluded.uploader,
                tagged_artist = excluded.tagged_artist,
                parsed_artist = excluded.parsed_artist,
                genre = excluded.genre,
                tags_json = excluded.tags_json,
                tags_text = excluded.tags_text,
                description = excluded.description,
                source_url = excluded.source_url,
                track_id = excluded.track_id,
                playlist = excluded.playlist,
                album_or_playlist_title = excluded.album_or_playlist_title,
                artwork_url = excluded.artwork_url,
                created_or_release_date = excluded.created_or_release_date,
                downloaded_at = excluded.downloaded_at,
                quality_result_json = excluded.quality_result_json,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                record_id,
                queue_item_id,
                output_path,
                record.get("title"),
                record.get("artist"),
                record.get("uploader"),
                record.get("tagged_artist"),
                record.get("parsed_artist"),
                record.get("genre"),
                json.dumps(tags),
                tags_text,
                record.get("description"),
                record.get("source_url"),
                record.get("track_id"),
                record.get("playlist"),
                record.get("album_or_playlist_title"),
                record.get("artwork_url"),
                record.get("created_or_release_date"),
                float(record.get("downloaded_at") or now),
                json.dumps(record.get("quality_result") or {}),
                json.dumps(record, default=str),
                now,
            ),
        )
        conn.commit()
    record["id"] = record_id
    return record


def row_to_item(row: sqlite3.Row) -> QueueItem:
    preset_id = row["preset_id"]
    target_url = row["target_url"] or row["target"]
    try:
        command, masked, archive_enabled = build_scdl_args(
            preset_id,
            target_url,
            archive_enabled=bool(row["archive_enabled"]),
            profile_type=row["profile_type"],
        )
    except HTTPException:
        command, masked, archive_enabled = [], [], bool(row["archive_enabled"])
    return QueueItem(
        id=row["id"],
        preset_id=preset_id,
        preset_name=row["preset_name"],
        target=row["target"],
        target_url=target_url,
        url_kind=row["url_kind"],
        profile_type=row["profile_type"],
        job_type=row["job_type"],
        command=command,
        masked_command=masked,
        archive_enabled=archive_enabled,
        is_likes_sync=bool(row["is_likes_sync"]),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        return_code=row["return_code"],
        output_file=row["output_file"],
        track_id=row["track_id"],
        last_error=row["last_error"],
        rate_limit_count=int(row["rate_limit_count"] or 0),
        last_rate_limit_backoff=row["last_rate_limit_backoff"],
        rate_limit_retry_at=row["rate_limit_retry_at"],
        log_path=Path(row["log_path"] or LOG_DIR / f"{row['id']}.log"),
        files=json_loads(row["files_json"], []),
        summary=json_loads(row["summary_json"], {}),
        metadata_records=metadata_for_queue(row["id"]),
    )


def persist_item(item: QueueItem) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO queue_items (
                id, preset_id, preset_name, target, target_url, url_kind, profile_type,
                job_type, status, archive_enabled,
                is_likes_sync, created_at, updated_at, started_at, finished_at,
                return_code, log_path, files_json, summary_json, output_file,
                track_id, last_error, rate_limit_count, last_rate_limit_backoff,
                rate_limit_retry_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                preset_id = excluded.preset_id,
                preset_name = excluded.preset_name,
                target = excluded.target,
                target_url = excluded.target_url,
                url_kind = excluded.url_kind,
                profile_type = excluded.profile_type,
                job_type = excluded.job_type,
                status = excluded.status,
                archive_enabled = excluded.archive_enabled,
                is_likes_sync = excluded.is_likes_sync,
                updated_at = excluded.updated_at,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                return_code = excluded.return_code,
                log_path = excluded.log_path,
                files_json = excluded.files_json,
                summary_json = excluded.summary_json,
                output_file = excluded.output_file,
                track_id = excluded.track_id,
                last_error = excluded.last_error,
                rate_limit_count = excluded.rate_limit_count,
                last_rate_limit_backoff = excluded.last_rate_limit_backoff,
                rate_limit_retry_at = excluded.rate_limit_retry_at
            """,
            (
                item.id,
                item.preset_id,
                item.preset_name,
                item.target,
                item.target_url,
                item.url_kind,
                item.profile_type,
                item.job_type,
                item.status,
                int(item.archive_enabled),
                int(item.is_likes_sync),
                item.created_at,
                item.updated_at,
                item.started_at,
                item.finished_at,
                item.return_code,
                str(item.log_path),
                json.dumps(item.files),
                json.dumps(item.summary),
                item.output_file,
                item.track_id,
                item.last_error,
                item.rate_limit_count,
                item.last_rate_limit_backoff,
                item.rate_limit_retry_at,
            ),
        )
        conn.commit()


def load_active_items() -> list[QueueItem]:
    init_db()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM queue_items
            WHERE status IN ('Pending', 'Running', ?)
            ORDER BY created_at ASC
            """,
            (RATE_LIMITED_STATUS,),
        ).fetchall()
    return [row_to_item(row) for row in rows]


def history_status_filter(status: str) -> tuple[str, list[Any]]:
    normalized = status.strip().lower()
    if normalized in {"downloaded", "done"}:
        return "status = ?", ["Done"]
    if normalized in {"failed", "skipped", "pending", "running", "cancelled"}:
        return "status = ?", [normalized.title()]
    if normalized in {"rate limited", "rate-limited", "paused - rate limited"}:
        return "status = ?", [RATE_LIMITED_STATUS]
    if normalized == "remaining":
        return "status IN ('Pending', 'Running')", []
    return "1 = 1", []


def history_query(status: str = "All", search: str = "", page: int = 1, page_size: int = 25) -> dict[str, Any]:
    init_db()
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    where, params = history_status_filter(status)
    if search.strip():
        if load_settings().get("search_tags_enabled", True):
            where += """
                AND (
                    target LIKE ? OR target_url LIKE ? OR preset_name LIKE ? OR output_file LIKE ? OR last_error LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM metadata_records
                        WHERE metadata_records.queue_item_id = queue_items.id
                        AND (
                            title LIKE ? OR artist LIKE ? OR uploader LIKE ? OR tagged_artist LIKE ?
                            OR parsed_artist LIKE ? OR genre LIKE ? OR tags_text LIKE ? OR playlist LIKE ?
                            OR source_url LIKE ? OR track_id LIKE ? OR album_or_playlist_title LIKE ?
                        )
                    )
                )
            """
            needle = f"%{search.strip()}%"
            params.extend([needle] * 16)
        else:
            where += " AND (target LIKE ? OR target_url LIKE ? OR preset_name LIKE ? OR output_file LIKE ? OR last_error LIKE ?)"
            needle = f"%{search.strip()}%"
            params.extend([needle] * 5)
    offset = (page - 1) * page_size
    with db_connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM queue_items WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT * FROM queue_items
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [history_row_public(row) for row in rows],
    }


def history_row_public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "preset_id": row["preset_id"],
        "preset_name": row["preset_name"],
        "target": row["target"],
        "target_url": row["target_url"],
        "url_kind": row["url_kind"],
        "profile_type": row["profile_type"],
        "job_type": row["job_type"],
        "status": row["status"],
        "archive_enabled": bool(row["archive_enabled"]),
        "is_likes_sync": bool(row["is_likes_sync"]),
        "created_at": iso_time(row["created_at"]),
        "updated_at": iso_time(row["updated_at"]),
        "started_at": iso_time(row["started_at"]),
        "finished_at": iso_time(row["finished_at"]),
        "return_code": row["return_code"],
        "output_file": row["output_file"],
        "track_id": row["track_id"],
        "last_error": row["last_error"],
        "rate_limit_count": row["rate_limit_count"],
        "last_rate_limit_backoff": row["last_rate_limit_backoff"],
        "rate_limit_retry_at": iso_time(row["rate_limit_retry_at"]),
        "summary": json_loads(row["summary_json"], {}),
        "metadata_records": metadata_for_queue(row["id"], limit=8),
    }


def app_stats() -> dict[str, Any]:
    init_db()
    with db_connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM queue_items GROUP BY status").fetchall()
        history_count = conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0]
        metadata_count = conn.execute("SELECT COUNT(*) FROM metadata_records").fetchone()[0]
        likes = conn.execute(
            """
            SELECT * FROM queue_items
            WHERE is_likes_sync = 1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
        ).fetchone()
        recent_failures = conn.execute(
            """
            SELECT * FROM queue_items
            WHERE status IN ('Failed', ?)
            ORDER BY updated_at DESC
            LIMIT 5
            """,
            (RATE_LIMITED_STATUS,),
        ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    downloaded = counts.get("Done", 0)
    failed = counts.get("Failed", 0)
    skipped = counts.get("Skipped", 0)
    pending = counts.get("Pending", 0)
    running = counts.get("Running", 0)
    rate_limited = counts.get(RATE_LIMITED_STATUS, 0)
    return {
        "history_count": history_count,
        "metadata_count": metadata_count,
        "archive_count": archive_count(),
        "total_processed": downloaded + failed + skipped,
        "downloaded": downloaded,
        "failed": failed,
        "skipped": skipped,
        "pending": pending,
        "running": running,
        "rate_limited": rate_limited,
        "remaining_unknown": pending + running,
        "latest_likes_sync": history_row_public(likes) if likes else None,
        "recent_failures": [history_row_public(row) for row in recent_failures],
    }


def retryable_likes_items() -> list[QueueItem]:
    init_db()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM queue_items
            WHERE is_likes_sync = 1 AND status IN ('Failed', ?)
            ORDER BY updated_at ASC
            """,
            (RATE_LIMITED_STATUS,),
        ).fetchall()
    return [row_to_item(row) for row in rows]


def load_settings() -> dict[str, Any]:
    ensure_directories()
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update(stored)
        except json.JSONDecodeError:
            backup = SETTINGS_PATH.with_suffix(".json.bak")
            SETTINGS_PATH.replace(backup)
    if settings.get("default_preset") not in PRESETS:
        settings["default_preset"] = "best-original"
    if settings.get("organization_mode") not in ORGANIZATION_MODES:
        settings["organization_mode"] = "library-clean"
    if settings.get("artist_metadata_priority") not in ARTIST_PRIORITY_MODES:
        settings["artist_metadata_priority"] = "smart-auto"
    if "use_playlist_folders" not in settings:
        settings["use_playlist_folders"] = not bool(settings.get("no_playlist_folder"))
    settings["no_playlist_folder"] = not bool(settings.get("use_playlist_folders", True))
    try:
        settings["max_concurrent_downloads"] = max(1, int(settings.get("max_concurrent_downloads") or 1))
    except (TypeError, ValueError):
        settings["max_concurrent_downloads"] = 1
    try:
        settings["download_delay_seconds"] = max(0.0, float(settings.get("download_delay_seconds") or 0))
    except (TypeError, ValueError):
        settings["download_delay_seconds"] = 2
    try:
        settings["max_rate_limit_backoff_seconds"] = max(1, int(settings.get("max_rate_limit_backoff_seconds") or 900))
    except (TypeError, ValueError):
        settings["max_rate_limit_backoff_seconds"] = 900
    try:
        settings["max_consecutive_rate_limits"] = max(1, int(settings.get("max_consecutive_rate_limits") or 8))
    except (TypeError, ValueError):
        settings["max_consecutive_rate_limits"] = 8
    if settings.get("default_profile_download_type") not in PROFILE_DOWNLOAD_TYPES:
        settings["default_profile_download_type"] = "uploads"
    save_settings(settings)
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    ensure_directories()
    safe_settings = dict(DEFAULT_SETTINGS)
    safe_settings.update(settings)
    SETTINGS_PATH.write_text(json.dumps(safe_settings, indent=2), encoding="utf-8")


def public_settings() -> dict[str, Any]:
    settings = load_settings()
    stored_token = str(settings.get("auth_token") or "").strip()
    env_token = str(os.environ.get("SOUNDCLOUD_AUTH_TOKEN") or "").strip()
    source = "settings" if stored_token else "environment" if env_token else "none"
    public = {key: value for key, value in settings.items() if key != "auth_token"}
    public.update(
        {
            "auth_configured": bool(stored_token or env_token),
            "auth_source": source,
            "masked_auth_token": TOKEN_MASK if stored_token or env_token else "",
            "download_dir": str(DOWNLOAD_DIR),
            "config_dir": str(CONFIG_DIR),
            "archive_path": str(ARCHIVE_PATH),
            "history_path": str(DB_PATH),
            "logs_dir": str(LOG_DIR),
            "organization_modes": ORGANIZATION_MODES,
            "artist_priority_modes": ARTIST_PRIORITY_MODES,
            "profile_download_types": PROFILE_DOWNLOAD_TYPES,
            "organization_preview": organization_preview(settings),
        },
    )
    return public


def get_auth_token(settings: Optional[dict[str, Any]] = None) -> str:
    settings = settings or load_settings()
    return str(settings.get("auth_token") or os.environ.get("SOUNDCLOUD_AUTH_TOKEN") or "").strip()


def split_urls(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        raw_parts = value
    else:
        raw_parts = re.split(r"[\s,]+", value.strip())
    return [part.strip() for part in raw_parts if part and part.strip()]


def soundcloud_path_parts(url: str) -> list[str]:
    parsed = urlparse(url)
    return [unquote(part) for part in parsed.path.strip("/").split("/") if part]


def classify_soundcloud_url(raw_url: str) -> dict[str, Any]:
    try:
        url = validate_soundcloud_url(raw_url)
    except HTTPException as exc:
        return {
            "valid": False,
            "url": raw_url,
            "kind": "invalid",
            "label": "Unknown",
            "message": str(exc.detail),
            "default_profile_type": None,
            "is_track": False,
            "is_playlist": False,
            "is_profile": False,
        }
    parts = soundcloud_path_parts(url)
    kind = "unknown"
    label = "Unknown"
    message = "Please paste a valid SoundCloud URL."
    default_profile_type = None
    if len(parts) >= 3 and parts[1] == "sets":
        kind = "playlist"
        label = "Playlist"
        message = "Playlist detected. Playlist downloads will use playlist folder formatting."
    elif len(parts) == 1:
        kind = "profile"
        label = "Profile"
        default_profile_type = "uploads"
        message = "Profile detected. Choose what to download."
    elif len(parts) == 2 and parts[1] in PROFILE_PATH_DEFAULTS:
        default_profile_type = PROFILE_PATH_DEFAULTS[parts[1]]
        kind = {
            "tracks": "profile-tracks",
            "popular-tracks": "profile-tracks",
            "likes": "profile-likes",
            "sets": "profile-sets",
            "albums": "profile-sets",
            "reposts": "profile-reposts",
        }.get(parts[1], "profile")
        label = {
            "profile-tracks": "Profile uploads",
            "profile-likes": "Profile likes",
            "profile-sets": "Profile playlists",
            "profile-reposts": "Profile reposts",
        }.get(kind, "Profile")
        message = "Profile detected. Choose what to download."
    elif len(parts) >= 2:
        kind = "track"
        label = "Track"
        message = "Track detected."
    return {
        "valid": kind != "unknown",
        "url": url,
        "kind": kind,
        "label": label,
        "message": message,
        "default_profile_type": default_profile_type,
        "profile_types": PROFILE_DOWNLOAD_TYPES,
        "is_track": kind == "track",
        "is_playlist": kind == "playlist",
        "is_profile": kind.startswith("profile"),
    }


def normalize_profile_type(profile_type: Optional[str], url_info: dict[str, Any], settings: dict[str, Any]) -> Optional[str]:
    if not url_info.get("is_profile"):
        return None
    selected = profile_type or url_info.get("default_profile_type") or settings.get("default_profile_download_type") or "uploads"
    selected = str(selected or "").strip()
    if selected not in PROFILE_DOWNLOAD_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Profile URLs require a download type. Choose Uploads, All Tracks + Reposts, Likes, Playlists, or Reposts.",
        )
    return selected


def job_type_for(preset: Preset, target: str, url_info: dict[str, Any], profile_type: Optional[str]) -> str:
    if preset.id == "likes-best":
        return "My Likes Sync"
    if profile_type:
        return str(PROFILE_DOWNLOAD_TYPES[profile_type]["job_type"])
    if url_info.get("kind") == "track":
        return "Track"
    if url_info.get("kind") == "playlist":
        return "Playlist"
    return preset.name


def job_context(preset: Preset, target: str, profile_type: Optional[str]) -> dict[str, Any]:
    if not preset.needs_url:
        return {"url_kind": "me-likes", "profile_type": None, "job_type": "My Likes Sync"}
    normalized = validate_soundcloud_url(target)
    info = classify_soundcloud_url(normalized)
    if preset.id == "profile-uploads" and not info.get("is_profile"):
        raise HTTPException(status_code=400, detail="Profile Uploads / Tracks is for SoundCloud profile URLs.")
    selected_profile_type = normalize_profile_type(profile_type, info, load_settings())
    if preset.id == "profile-uploads" and info.get("is_profile"):
        selected_profile_type = "uploads"
    return {
        "url_kind": info.get("kind", "unknown"),
        "profile_type": selected_profile_type,
        "job_type": job_type_for(preset, normalized, info, selected_profile_type),
    }


def validate_soundcloud_url(raw_url: str) -> str:
    candidate = raw_url.strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="SoundCloud URL is required")
    if candidate.startswith(("soundcloud.com/", "www.soundcloud.com/", "m.soundcloud.com/")):
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    allowed_hosts = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts:
        raise HTTPException(status_code=400, detail=f"Only soundcloud.com URLs are allowed: {raw_url}")
    if not parsed.path or parsed.path == "/":
        raise HTTPException(status_code=400, detail="SoundCloud URL must include a track, playlist, or user path")
    host = "soundcloud.com" if host in {"www.soundcloud.com", "m.soundcloud.com"} else host
    return urlunparse(("https", host, parsed.path, "", parsed.query, ""))


def scdl_command() -> str:
    return os.environ.get("SCDL_COMMAND", "scdl")


def safe_path_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(value or "")).strip(" .")
    return clean[:80] or "soundcloud"


def safe_filename_stem(value: str, fallback: str = "track", max_length: int = 120) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(" .")
    clean = re.sub(r"\s+", " ", clean)
    return clean[:max_length].rstrip(" .") or fallback


def normalize_tags(value: Any) -> list[str]:
    raw: list[Any]
    if value is None:
        raw = []
    elif isinstance(value, str):
        quoted = re.findall(r'"([^"]+)"', value)
        raw = quoted if quoted else re.split(r"[,\n;]+|\s{2,}", value)
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    tags: list[str] = []
    seen: set[str] = set()
    for part in raw:
        tag = str(part or "").strip().strip("#")
        if not tag:
            continue
        lowered = tag.lower()
        if lowered not in seen:
            tags.append(tag[:80])
            seen.add(lowered)
    return tags[:80]


def source_slug(target: str) -> str:
    if target == "me likes":
        return "likes"
    parsed = urlparse(target)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    return parts[0] if parts else "soundcloud"


def source_type_for(preset_id: str, target: str) -> str:
    if preset_id == "likes-best" or target == "me likes":
        return "likes"
    parsed = urlparse(target)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if preset_id == "profile-uploads" or len(parts) == 1 or (len(parts) == 2 and parts[1] in PROFILE_PATH_DEFAULTS):
        return "profile"
    if "sets" in parts or preset_id == "playlist-best":
        return "playlist"
    return "single"


def is_playlist_target(preset_id: str, target: str) -> bool:
    return source_type_for(preset_id, target) == "playlist"


def use_playlist_folders(settings: dict[str, Any]) -> bool:
    return bool(settings.get("use_playlist_folders", not settings.get("no_playlist_folder")))


def organization_download_dir(preset_id: str, target: str, settings: dict[str, Any]) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return DOWNLOAD_DIR.resolve()


def scdl_default_name_format(settings: dict[str, Any]) -> str:
    parts = []
    if settings.get("include_upload_date_in_filename"):
        parts.append("{timestamp}")
    parts.append("{user[username]} - {title}")
    if settings.get("include_track_id_in_filename"):
        parts[-1] = parts[-1] + " [{id}]"
    return " - ".join(parts)


def scdl_default_playlist_name_format(settings: dict[str, Any]) -> str:
    parts = []
    if settings.get("include_upload_date_in_filename"):
        parts.append("{timestamp}")
    parts.append("{playlist[tracknumber]} - {user[username]} - {title}")
    if settings.get("include_track_id_in_filename"):
        parts[-1] = parts[-1] + " [{id}]"
    return " - ".join(parts)


def organization_preview(settings: dict[str, Any]) -> list[str]:
    mode = str(settings.get("organization_mode") or "library-clean")
    if mode == "flat":
        return ["J Dilla - Song Title.flac", "Artist - Track.opus", "Uploader - DJ Edit.mp3"]
    if mode == "by-artist":
        return ["Artists/J Dilla/Song Title.flac", "Artists/Artist/Track.opus"]
    if mode == "by-playlist":
        return ["Playlists/Beat Tape/001 - Artist - Track.opus", "Singles/Artist - Track.mp3"]
    if mode == "by-source-type":
        return [
            "Likes/J Dilla/Song Title.flac",
            "Playlists/Beat Tape/001 - Artist - Track.opus",
            "Singles/Artist/Track.mp3",
            "Profiles/Profile Name/Upload Title.m4a",
        ]
    if mode == "scdl-default":
        return ["scdl chooses the original output folders and filenames"]
    return [
        "Likes/J Dilla/Song Title.flac",
        "Playlists/Beat Tape/001 - Artist - Track.opus",
        "Artists/Artist/Track.mp3",
        "Profiles/Profile Name/Uploads/Track Title.m4a",
    ]


def mask_command(command: list[str], token: str = "") -> list[str]:
    masked: list[str] = []
    hide_next = False
    for part in command:
        if hide_next:
            masked.append(TOKEN_MASK)
            hide_next = False
            continue
        masked.append(part)
        if part == "--auth-token":
            hide_next = True
    if token:
        masked = [part.replace(token, TOKEN_MASK) for part in masked]
    return masked


def mask_text(text: str, token: str = "") -> str:
    if token:
        text = text.replace(token, TOKEN_MASK)
    env_token = os.environ.get("SOUNDCLOUD_AUTH_TOKEN")
    if env_token:
        text = text.replace(env_token, TOKEN_MASK)
    return text


def build_scdl_args(
    preset_id: str,
    target: str,
    *,
    archive_enabled: Optional[bool] = None,
    profile_type: Optional[str] = None,
) -> tuple[list[str], list[str], bool]:
    settings = load_settings()
    preset = PRESETS.get(preset_id)
    if not preset:
        raise HTTPException(status_code=400, detail="Unknown preset")

    url_info: dict[str, Any] = {"kind": "me-likes", "is_profile": False, "is_track": False, "is_playlist": False}
    selected_profile_type: Optional[str] = None
    if preset.needs_url:
        target = validate_soundcloud_url(target)
        url_info = classify_soundcloud_url(target)
        if preset.id == "check-qualities" and not url_info.get("is_track"):
            raise HTTPException(
                status_code=400,
                detail="Check Qualities is for individual track URLs. For profiles or playlists, choose a download type and start a download.",
            )
        if preset.id == "profile-uploads" and not url_info.get("is_profile"):
            raise HTTPException(status_code=400, detail="Profile Uploads / Tracks is for SoundCloud profile URLs.")
        selected_profile_type = normalize_profile_type(profile_type, url_info, settings)
        if preset.id == "profile-uploads" and url_info.get("is_profile"):
            selected_profile_type = "uploads"
    else:
        target = "me likes"
        if not get_auth_token(settings):
            raise HTTPException(status_code=400, detail="A SoundCloud auth token is required for My Likes Sync")

    if selected_profile_type:
        args = ["-l", target, str(PROFILE_DOWNLOAD_TYPES[selected_profile_type]["flag"]), "--best-quality"]
    else:
        args = []
        for arg in preset.args:
            args.append(target if arg == "{url}" else arg)

    should_use_archive = settings.get("archive_enabled", True) if archive_enabled is None else archive_enabled
    if preset.id == "likes-best":
        should_use_archive = True

    if preset.downloads:
        target_download_dir = organization_download_dir(preset.id, target, settings)
        args.extend(["--path", str(target_download_dir)])
        if should_use_archive:
            args.extend(["--download-archive", str(ARCHIVE_PATH)])
        args.extend(["-c", "--retries", "3"])

        name_format = str(settings.get("name_format") or "").strip() or scdl_default_name_format(settings)
        if name_format:
            args.extend(["--name-format", name_format])

        playlist_format = str(settings.get("playlist_name_format") or "").strip() or scdl_default_playlist_name_format(settings)
        if playlist_format:
            args.extend(["--playlist-name-format", playlist_format])

        if not use_playlist_folders(settings):
            args.append("--no-playlist-folder")
        force_requested = settings.get("force_metadata") or "--force-metadata" in args
        if settings.get("preserve_original_metadata") and not force_requested:
            args.append("--original-metadata")
        if settings.get("force_metadata") and "--force-metadata" not in args:
            args.append("--force-metadata")
        if selected_profile_type and "--original-art" not in args:
            args.append("--original-art")
        if settings.get("original_art") and "--original-art" not in args:
            args.append("--original-art")
        if settings.get("add_description"):
            args.append("--add-description")

    token = get_auth_token(settings)
    if token:
        args.extend(["--auth-token", token])

    command = [scdl_command(), *args]
    return command, mask_command(command, token), bool(should_use_archive)


def snapshot_files() -> dict[str, tuple[int, int]]:
    ensure_directories()
    snapshot: dict[str, tuple[int, int]] = {}
    for path in DOWNLOAD_DIR.rglob("*"):
        if not path.is_file() or path.name.endswith(".scdl.lock"):
            continue
        try:
            relative = path.relative_to(DOWNLOAD_DIR).as_posix()
            stat = path.stat()
            snapshot[relative] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
    return snapshot


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    relative = path.relative_to(DOWNLOAD_DIR).as_posix()
    folder = path.parent.relative_to(DOWNLOAD_DIR).as_posix()
    info = {
        "name": path.name,
        "extension": path.suffix.lower().lstrip(".") or "file",
        "size": stat.st_size,
        "size_label": human_size(stat.st_size),
        "modified": iso_time(stat.st_mtime),
        "folder": "." if folder == "." else folder,
        "path": relative,
    }
    metadata = metadata_by_output_path(relative)
    if metadata:
        info["metadata"] = metadata
    return info


def first_tag_value(tags: Any, keys: list[str]) -> Optional[str]:
    if not tags:
        return None
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if isinstance(value, (list, tuple)) and value:
            return str(value[0]).strip() or None
        if value:
            return str(value).strip() or None
    return None


def read_embedded_metadata(path: Path) -> dict[str, Any]:
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        return {}
    try:
        from mutagen import File as MutagenFile

        easy = MutagenFile(path, easy=True)
        raw = MutagenFile(path, easy=False)
    except Exception:
        return {}
    easy_tags = getattr(easy, "tags", None)
    raw_tags = getattr(raw, "tags", None)
    tags = normalize_tags(
        first_tag_value(easy_tags, ["keywords", "grouping", "genre"])
        or first_tag_value(raw_tags, ["SOUNDCLOUD_TAGS", "TXXX:SOUNDCLOUD_TAGS"])
    )
    return {
        "title": first_tag_value(easy_tags, ["title"]) or first_tag_value(raw_tags, ["TIT2", "\xa9nam"]),
        "tagged_artist": first_tag_value(easy_tags, ["artist"]) or first_tag_value(raw_tags, ["TPE1", "\xa9ART"]),
        "album_artist": first_tag_value(easy_tags, ["albumartist"]) or first_tag_value(raw_tags, ["TPE2", "aART"]),
        "album_or_playlist_title": first_tag_value(easy_tags, ["album"]) or first_tag_value(raw_tags, ["TALB", "\xa9alb"]),
        "genre": first_tag_value(easy_tags, ["genre"]) or first_tag_value(raw_tags, ["TCON", "\xa9gen"]),
        "description": first_tag_value(easy_tags, ["description", "comment"]) or first_tag_value(raw_tags, ["COMM", "\xa9cmt"]),
        "source_url": first_tag_value(easy_tags, ["website", "url"]) or first_tag_value(raw_tags, ["WOAF", "WWWAUDIOFILE"]),
        "created_or_release_date": first_tag_value(easy_tags, ["date"]) or first_tag_value(raw_tags, ["TDRC", "TDAT", "\xa9day"]),
        "track_number": first_tag_value(easy_tags, ["tracknumber"]) or first_tag_value(raw_tags, ["TRCK", "trkn"]),
        "tags": tags,
    }


def parse_artist_title(value: str) -> dict[str, Any]:
    title = str(value or "").strip()
    if not title:
        return {"parsed_artist": None, "clean_title": None, "candidates": []}
    candidates: list[str] = []
    clean_title = title
    delimiter_match = re.match(r"^\s*(?P<artist>.{2,90}?)\s+(?:-|\u2013|\u2014|:)\s+(?P<title>.{2,180})\s*$", title)
    if delimiter_match:
        candidates.append(delimiter_match.group("artist").strip())
        clean_title = delimiter_match.group("title").strip()
    remix_match = re.search(
        r"[\(\[]\s*(?P<artist>[A-Za-z0-9&.,' _-]{2,80}?)\s+(?:remix|edit|refix|bootleg|flip|dub|rework|mashup)\s*[\)\]]",
        title,
        re.IGNORECASE,
    )
    if remix_match:
        candidates.append(remix_match.group("artist").strip())
    featured_match = re.search(r"\b(?:w/|with|feat\.?|ft\.?)\s+(?P<artist>[A-Za-z0-9&.,' _-]{2,80})", title, re.IGNORECASE)
    if featured_match:
        candidates.append(featured_match.group("artist").strip())
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip(" -:[]()")
        if candidate and candidate.lower() not in seen:
            normalized.append(candidate)
            seen.add(candidate.lower())
    return {
        "parsed_artist": normalized[0] if normalized else None,
        "clean_title": clean_title,
        "candidates": normalized,
    }


def choose_artist(
    *,
    priority: str,
    tagged_artist: Optional[str],
    uploader: Optional[str],
    parsed_artist: Optional[str],
) -> str:
    values = {
        "tagged": tagged_artist,
        "uploader": uploader,
        "parsed": parsed_artist,
    }
    if priority == "uploader-first":
        order = ["uploader", "tagged", "parsed"]
    elif priority == "tagged-first":
        order = ["tagged", "parsed", "uploader"]
    elif priority == "title-parse-first":
        order = ["parsed", "tagged", "uploader"]
    else:
        order = ["tagged", "uploader", "parsed"]
    for key in order:
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return "Unknown Artist"


def resolve_source_metadata(target: str) -> dict[str, Any]:
    if target == "me likes" or not target.startswith(("http://", "https://")):
        return {}
    try:
        from soundcloud import SoundCloud

        client = SoundCloud(None, get_auth_token() or None)
        item = client.resolve(target)
    except Exception:
        return {}
    if item is None:
        return {}
    user = getattr(item, "user", None)
    created_at = getattr(item, "created_at", None)
    tags = normalize_tags(getattr(item, "tag_list", None))
    publisher = getattr(item, "publisher_metadata", None) or {}
    if isinstance(publisher, dict):
        publisher_artist = publisher.get("artist") or publisher.get("writer_composer")
    else:
        publisher_artist = getattr(publisher, "artist", None) or getattr(publisher, "writer_composer", None)
    playlist_title = getattr(item, "title", None) if "sets" in urlparse(target).path else None
    return {
        "title": getattr(item, "title", None),
        "uploader": getattr(user, "username", None),
        "tagged_artist": publisher_artist,
        "genre": getattr(item, "genre", None),
        "tags": tags,
        "description": getattr(item, "description", None),
        "source_url": getattr(item, "permalink_url", None) or target,
        "track_id": str(getattr(item, "id", "") or "") or None,
        "playlist": playlist_title,
        "album_or_playlist_title": playlist_title,
        "artwork_url": getattr(item, "artwork_url", None),
        "created_or_release_date": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or "") or None,
    }


def infer_title_from_path(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"^\d{1,4}\s*-\s*", "", stem)
    return stem.strip() or path.stem


def build_file_metadata(
    path: Path,
    item: QueueItem,
    source_metadata: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    embedded = read_embedded_metadata(path)
    source_type = source_type_for(item.preset_id, item.target_url)
    source_title = source_metadata.get("title") if source_type in {"single", "likes"} else None
    inferred_title = embedded.get("title") or source_title or infer_title_from_path(path)
    parsed = parse_artist_title(inferred_title) if settings.get("parse_artist_from_title", True) else {}
    uploader = source_metadata.get("uploader") or source_slug(item.target_url)
    tagged_artist = embedded.get("tagged_artist") or source_metadata.get("tagged_artist")
    parsed_artist = parsed.get("parsed_artist")
    artist = choose_artist(
        priority=str(settings.get("artist_metadata_priority") or "smart-auto"),
        tagged_artist=tagged_artist,
        uploader=uploader,
        parsed_artist=parsed_artist,
    )
    tags = normalize_tags([*normalize_tags(source_metadata.get("tags")), *normalize_tags(embedded.get("tags")), source_metadata.get("genre"), embedded.get("genre")])
    playlist = source_metadata.get("playlist")
    if not playlist and source_type == "playlist":
        try:
            playlist = next(part for part in path.relative_to(DOWNLOAD_DIR).parts if part not in {"Playlists", "Singles", "Artists", "Likes"})
        except Exception:
            playlist = None
    relative = path.relative_to(DOWNLOAD_DIR).as_posix()
    return {
        "output_path": relative,
        "title": parsed.get("clean_title") or inferred_title,
        "artist": artist,
        "uploader": uploader,
        "tagged_artist": tagged_artist,
        "parsed_artist": parsed_artist,
        "artist_candidates": parsed.get("candidates") or [],
        "genre": embedded.get("genre") or source_metadata.get("genre"),
        "tags": tags,
        "description": embedded.get("description") or source_metadata.get("description"),
        "source_url": embedded.get("source_url") or source_metadata.get("source_url") or (item.target_url if item.target_url != "me likes" else None),
        "track_id": source_metadata.get("track_id"),
        "playlist": playlist,
        "album_or_playlist_title": embedded.get("album_or_playlist_title") or source_metadata.get("album_or_playlist_title"),
        "artwork_url": source_metadata.get("artwork_url"),
        "created_or_release_date": embedded.get("created_or_release_date") or source_metadata.get("created_or_release_date"),
        "track_number": embedded.get("track_number"),
        "source_type": source_type,
        "downloaded_at": time.time(),
        "quality_result": item.summary,
    }


def set_easy_tag(tags: Any, key: str, value: Any, *, force: bool) -> None:
    if value is None or value == "":
        return
    try:
        current = tags.get(key)
        if force or not current:
            tags[key] = [str(value)]
    except Exception:
        return


def embed_metadata(path: Path, record: dict[str, Any], settings: dict[str, Any]) -> None:
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        return
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
    except Exception:
        return
    if audio is None:
        return
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception:
            return
    force = bool(settings.get("force_metadata")) or not bool(settings.get("preserve_original_metadata", True))
    set_easy_tag(audio.tags, "title", record.get("title"), force=force)
    set_easy_tag(audio.tags, "artist", record.get("artist"), force=force)
    set_easy_tag(audio.tags, "albumartist", record.get("uploader"), force=force)
    if record.get("playlist") or record.get("album_or_playlist_title"):
        set_easy_tag(audio.tags, "album", record.get("playlist") or record.get("album_or_playlist_title"), force=force)
    set_easy_tag(audio.tags, "genre", record.get("genre"), force=force)
    set_easy_tag(audio.tags, "date", record.get("created_or_release_date"), force=force)
    comment_parts = [record.get("description"), record.get("source_url")]
    if settings.get("embed_soundcloud_tags", True) and record.get("tags"):
        comment_parts.append("Tags: " + ", ".join(record["tags"]))
    set_easy_tag(audio.tags, "comment", "\n".join(str(part) for part in comment_parts if part), force=force)
    try:
        audio.save()
    except Exception:
        return


def playlist_index(record: dict[str, Any]) -> str:
    raw = str(record.get("track_number") or "").split("/", 1)[0]
    digits = re.sub(r"\D+", "", raw)
    return digits.zfill(3) if digits else "001"


def dated_prefix(record: dict[str, Any]) -> str:
    raw = str(record.get("created_or_release_date") or "")
    match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    return f"{match.group(0)} - " if match else ""


def destination_for_record(path: Path, record: dict[str, Any], item: QueueItem, settings: dict[str, Any]) -> Path:
    mode = str(settings.get("organization_mode") or "library-clean")
    if mode == "scdl-default":
        return path
    source_type = record.get("source_type") or source_type_for(item.preset_id, item.target_url)
    artist = safe_path_component(record.get("artist") or record.get("uploader") or "Unknown Artist")
    title = safe_filename_stem(record.get("title") or path.stem, fallback="Track")
    playlist = safe_path_component(record.get("playlist") or record.get("album_or_playlist_title") or "Unknown Playlist")
    uploader = safe_path_component(record.get("uploader") or "Unknown")
    profile_section = {
        "uploads": "Uploads",
        "all": "All Tracks + Reposts",
        "likes": "Likes",
        "playlists": "Playlists",
        "reposts": "Reposts",
    }.get(item.profile_type or "", "Uploads")
    track_id = safe_filename_stem(record.get("track_id") or "", fallback="", max_length=32)
    date_prefix = dated_prefix(record) if settings.get("include_upload_date_in_filename") else ""
    id_suffix = f" [{track_id}]" if settings.get("include_track_id_in_filename") and track_id else ""
    ext = path.suffix
    if source_type == "playlist":
        stem = f"{playlist_index(record)} - {artist} - {title}{id_suffix}"
    elif source_type == "profile" and item.profile_type == "playlists":
        stem = f"{playlist_index(record)} - {title}{id_suffix}"
    elif mode == "by-playlist":
        stem = f"{date_prefix}{artist} - {title}{id_suffix}"
    elif mode == "flat":
        stem = f"{date_prefix}{artist} - {title}{id_suffix}"
    else:
        stem = f"{date_prefix}{title}{id_suffix}"
    filename = safe_filename_stem(stem, fallback="Track", max_length=150) + ext

    if mode == "flat":
        folder = DOWNLOAD_DIR
    elif mode == "by-artist":
        folder = DOWNLOAD_DIR / "Artists" / artist
    elif mode == "by-playlist":
        folder = DOWNLOAD_DIR / "Playlists" / playlist if source_type == "playlist" and use_playlist_folders(settings) else DOWNLOAD_DIR / "Singles"
    elif mode == "by-source-type":
        if source_type == "likes":
            folder = DOWNLOAD_DIR / "Likes" / artist
        elif source_type == "playlist":
            folder = DOWNLOAD_DIR / "Playlists" / playlist if use_playlist_folders(settings) else DOWNLOAD_DIR / "Playlists"
        elif source_type == "profile":
            folder = DOWNLOAD_DIR / "Profiles" / uploader / profile_section
            if item.profile_type == "playlists" and use_playlist_folders(settings):
                folder = folder / playlist
        else:
            folder = DOWNLOAD_DIR / "Singles" / artist
    else:
        if source_type == "likes":
            folder = DOWNLOAD_DIR / "Likes" / artist if settings.get("put_likes_in_likes_folder", True) else DOWNLOAD_DIR / "Artists" / artist
        elif source_type == "playlist":
            folder = DOWNLOAD_DIR / "Playlists" / playlist if use_playlist_folders(settings) else DOWNLOAD_DIR / "Playlists"
        elif source_type == "profile":
            folder = DOWNLOAD_DIR / "Profiles" / uploader / profile_section
            if item.profile_type == "playlists" and use_playlist_folders(settings):
                folder = folder / playlist
        elif artist == "Unknown Artist":
            folder = DOWNLOAD_DIR / "Unknown" / uploader
        elif source_type == "single" and settings.get("put_singles_in_singles_folder", True):
            folder = DOWNLOAD_DIR / "Singles" / artist
        else:
            folder = DOWNLOAD_DIR / "Artists" / artist
    return folder / filename


def unique_destination(path: Path, record: dict[str, Any], item_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return path
    track_id = safe_filename_stem(record.get("track_id") or "", fallback="", max_length=24)
    suffix = track_id or item_id[:8] or uuid.uuid4().hex[:8]
    candidate = path.with_name(f"{path.stem} [{suffix}]{path.suffix}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} [{suffix}-{counter}]{path.suffix}")
        counter += 1
    return candidate


def move_related_files(source: Path, destination: Path) -> None:
    for related in source.parent.glob(source.stem + ".*"):
        if related == source or related.suffix.lower() not in RELATED_FILE_EXTENSIONS:
            continue
        target = destination.with_suffix(related.suffix)
        target = unique_destination(target, {"track_id": ""}, uuid.uuid4().hex[:8])
        try:
            shutil.move(str(related), str(target))
        except OSError:
            continue


def write_sidecar(record: dict[str, Any], path: Path) -> None:
    sidecar = path.with_suffix(path.suffix + ".json")
    sidecar.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def process_downloaded_metadata(
    item: QueueItem,
    source_metadata: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    processed_files: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for file in item.files:
        try:
            path = (DOWNLOAD_DIR / file["path"]).resolve()
            path.relative_to(DOWNLOAD_DIR)
        except Exception:
            continue
        if not path.exists():
            continue
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            processed_files.append(file_info(path))
            continue
        record = build_file_metadata(path, item, source_metadata, settings)
        destination = destination_for_record(path, record, item, settings)
        destination = unique_destination(destination, record, item.id) if destination != path else path
        if destination != path:
            move_related_files(path, destination)
            shutil.move(str(path), str(destination))
            path = destination
            record["output_path"] = path.relative_to(DOWNLOAD_DIR).as_posix()
        embed_metadata(path, record, settings)
        if settings.get("save_sidecar_json"):
            write_sidecar(record, path)
        saved = upsert_metadata_record(item.id, record)
        records.append(saved)
        processed_files.append(file_info(path))
    return processed_files, records


def new_or_changed_files(before: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    changed: list[Path] = []
    for path in DOWNLOAD_DIR.rglob("*"):
        if not path.is_file() or path.name.endswith(".scdl.lock"):
            continue
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            relative = path.relative_to(DOWNLOAD_DIR).as_posix()
            stat = path.stat()
            if before.get(relative) != (stat.st_size, stat.st_mtime_ns):
                changed.append(path)
        except OSError:
            continue
    changed.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [file_info(path) for path in changed[:RECENT_FILE_LIMIT]]


def recent_files() -> list[dict[str, Any]]:
    ensure_directories()
    files: list[Path] = []
    for path in DOWNLOAD_DIR.rglob("*"):
        if not path.is_file() or path.name.endswith(".scdl.lock"):
            continue
        if path.suffix.lower() in MEDIA_EXTENSIONS:
            files.append(path)
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [file_info(path) for path in files[:RECENT_FILE_LIMIT]]


def summarize_logs(logs: list[str], files: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(logs)
    lower = text.lower()
    extensions = sorted({item["extension"].lower() for item in files})
    skipped = "already downloaded" in lower or "download archive" in lower and "skip" in lower
    original_attempted = "downloading the original file" in lower
    original_missing = "could not get original download link" in lower
    original_used = original_attempted and not original_missing and not skipped
    fallback_used = original_missing or (not original_used and any(ext in {"mp3", "m4a", "opus"} for ext in extensions))
    lossless = original_used and any(ext in {"flac", "wav", "aif", "aiff"} for ext in extensions)

    badges: list[str] = []
    if skipped:
        badges.append("Skipped / Already downloaded")
    if original_used:
        badges.append("Original Found")
    if lossless:
        badges.append("Lossless")
    if original_used and "flac" in extensions:
        badges.append("FLAC from original")
    if fallback_used and "opus" in extensions:
        badges.append("Opus fallback")
    if fallback_used and any(ext in {"mp3", "m4a"} for ext in extensions):
        badges.append("MP3/M4A fallback")
    if not badges:
        badges.append("Unknown quality")

    warning_lines = [
        line
        for line in logs
        if re.search(r"\b(error|warning|failed|unable|could not|not available)\b", line, re.IGNORECASE)
    ][-12:]

    return {
        "file_count": len(files),
        "files": files,
        "extensions": extensions,
        "original_or_lossless_used": original_used or lossless,
        "fallback_used": fallback_used,
        "skipped": skipped,
        "warnings": warning_lines,
        "badges": badges,
    }


def is_auth_related_error(text: str) -> bool:
    return bool(
        re.search(
            r"(invalid auth|auth token|unauthorized|forbidden|401|403|login required|oauth)",
            text,
            re.IGNORECASE,
        )
    )


def parse_rate_limit_backoff(line: str) -> Optional[int]:
    lower = line.lower()
    if "rate-limit" not in lower and "rate limited" not in lower and "429" not in lower:
        return None
    match = re.search(r"(?:delaying|backoff|retry(?:-after)?|retry after)[^\d]{0,30}(\d+)\s*(?:s|sec|secs|second|seconds)?", lower)
    if match:
        return int(match.group(1))
    retry_match = re.search(r"retry-after[^\d]{0,20}(\d+)", lower)
    if retry_match:
        return int(retry_match.group(1))
    return 0


def parse_rate_limit_reset(line: str) -> Optional[float]:
    retry_after = re.search(r"retry-after[^\d]{0,20}(\d+)", line, re.IGNORECASE)
    if retry_after:
        return time.time() + int(retry_after.group(1))
    reset = re.search(r"reset(?:_time)?[^\d]{0,20}(\d{10,})", line, re.IGNORECASE)
    if reset:
        value = int(reset.group(1))
        return value / 1000 if value > 9_999_999_999 else float(value)
    return None


def rate_limit_message(item: QueueItem) -> str:
    if item.rate_limit_retry_at:
        return f"SoundCloud rate-limited this job. Safe to resume after: {iso_time(item.rate_limit_retry_at)}"
    return "SoundCloud rate-limited this job. Try again later. Your downloaded tracks are saved and archive will skip completed tracks."


def parse_quality_output(output: str) -> dict[str, Any]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    qualities = []
    for line in lines:
        if " - " not in line or "(" not in line or ")" not in line:
            continue
        preset, rest = line.split(" - ", 1)
        mime = rest.split("(", 1)[0].strip()
        protocol = rest.rsplit("(", 1)[-1].rstrip(")")
        qualities.append({"preset": preset.strip(), "mime": mime, "protocol": protocol.strip()})

    lower = output.lower()
    badges: list[str] = []
    if "original download may be available" in lower:
        badges.append("Original possible")
    if any("opus" in item["preset"].lower() or "opus" in item["mime"].lower() for item in qualities):
        badges.append("Opus")
    if any("aac" in item["preset"].lower() or "mp4" in item["mime"].lower() for item in qualities):
        badges.append("M4A/AAC")
    if any("mp3" in item["preset"].lower() or "mpeg" in item["mime"].lower() for item in qualities):
        badges.append("MP3")
    if not badges:
        badges.append("Unknown quality")
    return {"qualities": qualities, "badges": badges, "raw": output}


class QueueManager:
    def __init__(self) -> None:
        self.items: list[QueueItem] = []
        self.lock = asyncio.Lock()
        self.paused = True
        self.stop_after_current = False
        self.subscribers: set[asyncio.Queue] = set()

    async def load_from_db(self) -> None:
        items = load_active_items()
        async with self.lock:
            self.items = items
            self.paused = True
            self.stop_after_current = False

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    async def broadcast(self, event: dict[str, Any]) -> None:
        for subscriber in list(self.subscribers):
            try:
                subscriber.put_nowait(event)
            except asyncio.QueueFull:
                self.subscribers.discard(subscriber)

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return {
                "paused": self.paused,
                "stop_after_current": self.stop_after_current,
                "max_concurrent_downloads": load_settings()["max_concurrent_downloads"],
                "items": [item.public() for item in self.items],
            }

    async def add(self, request: QueueAddRequest) -> list[QueueItem]:
        preset = PRESETS.get(request.preset)
        if not preset:
            raise HTTPException(status_code=400, detail="Unknown preset")
        if request.preset == "check-qualities":
            raise HTTPException(status_code=400, detail="Use Check Qualities for this preset")

        targets = ["me likes"] if not preset.needs_url else split_urls(request.urls)
        if preset.needs_url and not targets:
            raise HTTPException(status_code=400, detail="Add at least one SoundCloud URL")

        created: list[QueueItem] = []
        async with self.lock:
            if preset.id == "likes-best" and any(
                item.is_likes_sync and item.status in ACTIVE_STATUSES for item in self.items
            ):
                raise HTTPException(status_code=409, detail="A Likes Sync job is already pending or running")
            for target in targets:
                context = job_context(preset, target, request.profile_type)
                command, masked, archive_enabled = build_scdl_args(
                    request.preset,
                    target,
                    archive_enabled=request.archive_enabled,
                    profile_type=context["profile_type"],
                )
                job_id = uuid.uuid4().hex[:12]
                is_likes_sync = preset.id == "likes-best"
                item = QueueItem(
                    id=job_id,
                    preset_id=preset.id,
                    preset_name=context["job_type"] if context["profile_type"] else preset.name,
                    target=target if preset.needs_url else "My likes",
                    target_url=target if preset.needs_url else "me likes",
                    url_kind=context["url_kind"],
                    profile_type=context["profile_type"],
                    job_type=context["job_type"],
                    command=command,
                    masked_command=masked,
                    archive_enabled=archive_enabled,
                    is_likes_sync=is_likes_sync,
                    log_path=LOG_DIR / f"{job_id}.log",
                )
                self.items.append(item)
                persist_item(item)
                created.append(item)
            if request.autostart:
                self.paused = False
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
        if request.autostart:
            await self.kick()
        return created

    async def start(self) -> None:
        async with self.lock:
            self.paused = False
            self.stop_after_current = False
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
        await self.kick()

    async def pause(self) -> None:
        async with self.lock:
            self.paused = True
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})

    async def kick(self) -> None:
        async with self.lock:
            if self.paused or self.stop_after_current:
                return
            settings = load_settings()
            max_concurrent = max(1, int(settings["max_concurrent_downloads"]))
            running = [item for item in self.items if item.status == "Running"]
            available = max_concurrent - len(running)
            pending = [item for item in self.items if item.status == "Pending"]
            for item in pending[:available]:
                item.status = "Running"
                item.started_at = time.time()
                item.updated_at = item.started_at
                persist_item(item)
                item.task = asyncio.create_task(self.run_item(item))
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})

    async def append_log(self, item: QueueItem, text: str) -> None:
        token = get_auth_token()
        settings = load_settings()
        max_backoff = int(settings.get("max_rate_limit_backoff_seconds", 900))
        max_repeated = int(settings.get("max_consecutive_rate_limits", 8))
        clean = mask_text(text, token).replace("\r", "\n")
        lines = clean.splitlines() or [clean]
        with item.log_path.open("a", encoding="utf-8") as handle:
            for raw_line in lines:
                line = raw_line.rstrip()
                if not line:
                    continue
                backoff = parse_rate_limit_backoff(line)
                pause_due_to_repeated = False
                pause_due_to_cap = False
                if backoff is not None:
                    item.rate_limit_count += 1
                    capped = min(max(backoff, 0), max_backoff)
                    item.last_rate_limit_backoff = capped
                    item.rate_limit_retry_at = parse_rate_limit_reset(line) or (time.time() + capped if capped else None)
                    if backoff > max_backoff:
                        line = f"Rate limited. Backoff capped at {max_backoff}s."
                        pause_due_to_cap = True
                        item.rate_limit_pause_requested = True
                    elif backoff > 0:
                        line = f"Rate limited. Backoff {capped}s."
                    else:
                        line = "Rate limited."
                    if item.rate_limit_count >= max_repeated:
                        pause_due_to_repeated = True
                        item.rate_limit_pause_requested = True
                        item.last_error = "Repeated rate limits reached. Pausing job safely."
                elif re.search(r"\b(receiving|downloaded|saved|applying metadata|finished)\b", line, re.IGNORECASE):
                    item.rate_limit_count = 0
                item.logs.append(line)
                item.logs = item.logs[-MAX_LOG_LINES:]
                if re.search(
                    r"\b(error|failed|unable|could not|invalid auth|unauthorized|forbidden|rate limit|401|403|429)\b",
                    line,
                    re.IGNORECASE,
                ):
                    item.last_error = line[-500:]
                handle.write(line + "\n")
                await self.broadcast({"type": "log", "item_id": item.id, "line": line})
                if item.rate_limit_pause_requested:
                    pause_line = (
                        "Repeated rate limits reached. Pausing job safely."
                        if pause_due_to_repeated
                        else "Rate-limit backoff cap reached. Pausing job safely."
                    )
                    if pause_due_to_repeated or pause_due_to_cap:
                        if pause_line not in item.logs[-3:]:
                            item.logs.append(pause_line)
                            handle.write(pause_line + "\n")
                            await self.broadcast({"type": "log", "item_id": item.id, "line": pause_line})
                    resume_line = "Safe to resume later; archive will skip completed tracks."
                    if resume_line not in item.logs[-3:]:
                        item.logs.append(resume_line)
                        handle.write(resume_line + "\n")
                        await self.broadcast({"type": "log", "item_id": item.id, "line": resume_line})
        item.updated_at = time.time()

    async def run_item(self, item: QueueItem) -> None:
        before = snapshot_files()
        item.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            command, masked, archive_enabled = build_scdl_args(
                item.preset_id,
                item.target_url,
                archive_enabled=item.archive_enabled,
                profile_type=item.profile_type,
            )
            item.command = command
            item.masked_command = masked
            item.archive_enabled = archive_enabled
            persist_item(item)
        except HTTPException as exc:
            item.return_code = 1
            item.last_error = str(exc.detail)
            item.logs.append(item.last_error)
            item.status = "Failed"
            item.finished_at = time.time()
            item.updated_at = item.finished_at
            persist_item(item)
            await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
            await self.kick()
            return
        item.log_path.write_text(
            "Command: " + " ".join(item.masked_command) + "\n\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["XDG_CONFIG_HOME"] = str(CONFIG_DIR)
        try:
            process = await asyncio.create_subprocess_exec(
                *item.command,
                cwd=str(DOWNLOAD_DIR),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            item.process = process
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                await self.append_log(item, line.decode("utf-8", errors="replace"))
                if item.rate_limit_pause_requested:
                    await self.terminate_process(item)
                    break
            item.return_code = await process.wait()
        except asyncio.CancelledError:
            await self.terminate_process(item)
            item.status = "Cancelled"
            item.last_error = "Cancelled by user"
            await self.append_log(item, "Cancelled by user")
            raise
        except Exception as exc:
            item.return_code = 1
            item.last_error = f"Failed to start scdl: {exc}"
            await self.append_log(item, item.last_error)
        finally:
            item.process = None
            item.files = new_or_changed_files(before)
            if item.files:
                item.output_file = item.files[0].get("path")
            item.summary = summarize_logs(item.logs, item.files)
            item.finished_at = time.time()
            item.updated_at = item.finished_at
            if item.status != "Cancelled":
                if item.rate_limit_pause_requested:
                    item.status = RATE_LIMITED_STATUS
                    item.return_code = None
                    item.last_error = rate_limit_message(item)
                    item.summary.setdefault("warnings", []).append(item.last_error)
                    item.summary["rate_limited"] = True
                    item.summary["retry_after"] = iso_time(item.rate_limit_retry_at)
                    item.summary["last_backoff_seconds"] = item.last_rate_limit_backoff
                elif item.summary.get("skipped"):
                    item.status = "Skipped"
                elif item.return_code == 0:
                    item.status = "Done"
                else:
                    item.status = "Failed"
                    if not item.last_error and item.summary.get("warnings"):
                        item.last_error = item.summary["warnings"][-1]
            auth_error = item.status == "Failed" and is_auth_related_error(
                "\n".join([item.last_error or "", *item.logs[-40:]])
            )
            if auth_error:
                item.summary.setdefault("warnings", []).append("Authentication-related failure detected; queue paused.")
                item.last_error = item.last_error or "Authentication-related failure detected; queue paused."
            if item.files:
                settings = load_settings()
                source_metadata = await asyncio.to_thread(resolve_source_metadata, item.target_url)
                item.files, item.metadata_records = process_downloaded_metadata(item, source_metadata, settings)
                if item.files:
                    item.output_file = item.files[0].get("path")
                item.summary["files"] = item.files
                item.summary["metadata_records"] = item.metadata_records[:20]
            persist_item(item)
            async with self.lock:
                if auth_error or item.status == RATE_LIMITED_STATUS:
                    self.paused = True
                if self.stop_after_current:
                    self.paused = True
                    self.stop_after_current = False
            await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
            delay = load_settings().get("download_delay_seconds", 0)
            if delay and not self.paused:
                await asyncio.sleep(float(delay))
            await self.kick()

    async def terminate_process(self, item: QueueItem) -> None:
        process = item.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def cancel(self, item_id: Optional[str] = None) -> None:
        cancelled_pending = False
        async with self.lock:
            if item_id:
                pending_item = next(
                    (
                        candidate
                        for candidate in self.items
                        if candidate.id == item_id and candidate.status in {"Pending", RATE_LIMITED_STATUS}
                    ),
                    None,
                )
                if pending_item:
                    pending_item.status = "Cancelled"
                    pending_item.finished_at = time.time()
                    pending_item.updated_at = pending_item.finished_at
                    pending_item.last_error = "Stopped by user"
                    persist_item(pending_item)
                    cancelled_pending = True
            candidates = [item for item in self.items if item.status == "Running"]
            if item_id:
                candidates = [item for item in candidates if item.id == item_id]
            if cancelled_pending:
                task = None
            elif not candidates:
                raise HTTPException(status_code=404, detail="No running item found")
            else:
                item = candidates[0]
                task = item.task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})

    async def retry(self, item_id: str) -> None:
        async with self.lock:
            item = next((candidate for candidate in self.items if candidate.id == item_id), None)
            if not item:
                raise HTTPException(status_code=404, detail="Queue item not found")
            if item.status == "Running":
                raise HTTPException(status_code=409, detail="Cannot retry a running item")
            item.status = "Pending"
            item.return_code = None
            item.started_at = None
            item.finished_at = None
            item.logs = []
            item.files = []
            item.summary = {}
            item.last_error = None
            item.output_file = None
            item.rate_limit_count = 0
            item.last_rate_limit_backoff = None
            item.rate_limit_retry_at = None
            item.rate_limit_pause_requested = False
            item.updated_at = time.time()
            persist_item(item)
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
        await self.kick()

    async def retry_failed(self) -> None:
        async with self.lock:
            for item in self.items:
                if item.status in {"Failed", RATE_LIMITED_STATUS}:
                    item.status = "Pending"
                    item.return_code = None
                    item.started_at = None
                    item.finished_at = None
                    item.logs = []
                    item.files = []
                    item.summary = {}
                    item.last_error = None
                    item.output_file = None
                    item.rate_limit_count = 0
                    item.last_rate_limit_backoff = None
                    item.rate_limit_retry_at = None
                    item.rate_limit_pause_requested = False
                    item.updated_at = time.time()
                    persist_item(item)
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
        await self.kick()

    async def start_or_resume_likes_sync(self, retry_failed_only: bool = False) -> QueueItem:
        if not get_auth_token():
            raise HTTPException(status_code=400, detail="A SoundCloud auth token is required for My Likes Sync")

        async with self.lock:
            active = next(
                (item for item in self.items if item.is_likes_sync and item.status in ACTIVE_STATUSES),
                None,
            )
            if active:
                self.paused = False
                self.stop_after_current = False
                selected = active
            else:
                selected = None

        if selected:
            await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
            await self.kick()
            return selected

        failed = retryable_likes_items()
        if retry_failed_only:
            if not failed:
                raise HTTPException(status_code=404, detail="No failed Likes Sync jobs to retry")
            selected = failed[-1]
        elif failed:
            selected = failed[-1]

        if selected:
            selected.status = "Pending"
            selected.return_code = None
            selected.started_at = None
            selected.finished_at = None
            selected.logs = []
            selected.files = []
            selected.summary = {}
            selected.output_file = None
            selected.last_error = None
            selected.rate_limit_count = 0
            selected.last_rate_limit_backoff = None
            selected.rate_limit_retry_at = None
            selected.rate_limit_pause_requested = False
            selected.updated_at = time.time()
            persist_item(selected)
            async with self.lock:
                for index, item in enumerate(self.items):
                    if item.id == selected.id:
                        self.items[index] = selected
                        break
                else:
                    self.items.append(selected)
                self.paused = False
                self.stop_after_current = False
            await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
            await self.kick()
            return selected

        created = await self.add(
            QueueAddRequest(
                urls="",
                preset="likes-best",
                autostart=True,
                archive_enabled=True,
            ),
        )
        return created[0]

    async def stop_after_current_item(self) -> None:
        async with self.lock:
            self.stop_after_current = True
            pending = [item for item in self.items if item.status == "Pending"]
            if not any(item.status == "Running" for item in self.items):
                self.paused = True
                self.stop_after_current = False
            for item in pending:
                item.status = "Cancelled"
                item.finished_at = time.time()
                item.updated_at = item.finished_at
                item.last_error = "Cancelled by stop-after-current"
                persist_item(item)
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})

    async def clear_completed(self) -> None:
        async with self.lock:
            self.items = [item for item in self.items if item.status not in TERMINAL_STATUSES]
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})

    async def clear_all(self) -> None:
        async with self.lock:
            running = [item for item in self.items if item.status == "Running"]
        for item in running:
            await self.cancel(item.id)
        async with self.lock:
            for item in self.items:
                if item.status in ACTIVE_STATUSES:
                    item.status = "Cancelled"
                    item.finished_at = time.time()
                    item.updated_at = item.finished_at
                    item.last_error = "Cancelled by clear all"
                    persist_item(item)
            self.items = []
            self.paused = True
            self.stop_after_current = False
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})


queue_manager = QueueManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_directories()
    init_db()
    load_settings()
    await queue_manager.load_from_db()
    yield


app = FastAPI(title="SoundCloud Quality Downloader", version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/presets")
async def get_presets() -> dict[str, Any]:
    return {
        "default": load_settings()["default_preset"],
        "presets": [
            {
                "id": preset.id,
                "name": preset.name,
                "description": preset.description,
                "downloads": preset.downloads,
                "needs_url": preset.needs_url,
            }
            for preset in PRESETS.values()
        ],
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return public_settings()


@app.post("/api/url-info")
async def get_url_info(request: UrlInfoRequest) -> dict[str, Any]:
    return classify_soundcloud_url(request.url)


@app.put("/api/settings")
async def update_settings(update: SettingsUpdate) -> dict[str, Any]:
    settings = load_settings()
    data = update.model_dump(exclude_unset=True)
    if data.pop("clear_auth_token", False):
        settings["auth_token"] = ""
    if "auth_token" in data:
        token = str(data.pop("auth_token") or "").strip()
        if token and token != TOKEN_MASK:
            settings["auth_token"] = token
        elif token == "":
            settings["auth_token"] = ""
    for key, value in data.items():
        if key == "max_concurrent_downloads" and value is not None:
            settings[key] = max(1, int(value))
        elif key == "download_delay_seconds" and value is not None:
            settings[key] = max(0.0, float(value))
        elif key == "max_rate_limit_backoff_seconds" and value is not None:
            settings[key] = max(1, int(value))
        elif key == "max_consecutive_rate_limits" and value is not None:
            settings[key] = max(1, int(value))
        elif key == "default_preset" and value in PRESETS:
            settings[key] = value
        elif key == "default_profile_download_type" and value in PROFILE_DOWNLOAD_TYPES:
            settings[key] = value
        elif key == "organization_mode" and value in ORGANIZATION_MODES:
            settings[key] = value
        elif key == "artist_metadata_priority" and value in ARTIST_PRIORITY_MODES:
            settings[key] = value
        elif key == "use_playlist_folders" and value is not None:
            settings[key] = bool(value)
            settings["no_playlist_folder"] = not bool(value)
        elif key != "default_preset":
            settings[key] = value
    save_settings(settings)
    await queue_manager.kick()
    return public_settings()


def check_soundcloud_auth(token: str) -> dict[str, Any]:
    if not token:
        raise HTTPException(status_code=400, detail="No SoundCloud auth token is configured")
    try:
        from soundcloud import SoundCloud

        client = SoundCloud(None, token)
        if not client.is_auth_token_valid():
            return {"ok": False, "message": "SoundCloud rejected this auth token"}
        me = client.get_me()
        return {
            "ok": True,
            "message": "Auth token is valid",
            "user": getattr(me, "username", None),
            "user_id": getattr(me, "id", None),
        }
    except HTTPException:
        raise
    except Exception as exc:
        return {"ok": False, "message": f"Auth check failed: {mask_text(str(exc), token)}"}


@app.post("/api/auth/test")
async def test_auth() -> dict[str, Any]:
    return await asyncio.to_thread(check_soundcloud_auth, get_auth_token())


@app.get("/api/queue")
async def queue_state() -> dict[str, Any]:
    return await queue_manager.snapshot()


@app.post("/api/queue")
async def add_to_queue(request: QueueAddRequest) -> dict[str, Any]:
    items = await queue_manager.add(request)
    return {"items": [item.public() for item in items], "queue": await queue_manager.snapshot()}


@app.post("/api/queue/start")
async def start_queue() -> dict[str, Any]:
    await queue_manager.start()
    return await queue_manager.snapshot()


@app.post("/api/queue/pause")
async def pause_queue() -> dict[str, Any]:
    await queue_manager.pause()
    return await queue_manager.snapshot()


@app.post("/api/queue/resume")
async def resume_queue() -> dict[str, Any]:
    await queue_manager.start()
    return await queue_manager.snapshot()


@app.post("/api/queue/cancel-current")
async def cancel_current() -> dict[str, Any]:
    await queue_manager.cancel()
    return await queue_manager.snapshot()


@app.post("/api/queue/stop-after-current")
async def stop_after_current() -> dict[str, Any]:
    await queue_manager.stop_after_current_item()
    return await queue_manager.snapshot()


@app.post("/api/queue/{item_id}/cancel")
async def cancel_item(item_id: str) -> dict[str, Any]:
    await queue_manager.cancel(item_id)
    return await queue_manager.snapshot()


@app.post("/api/queue/{item_id}/retry")
async def retry_item(item_id: str) -> dict[str, Any]:
    await queue_manager.retry(item_id)
    return await queue_manager.snapshot()


@app.post("/api/queue/retry-failed")
async def retry_failed() -> dict[str, Any]:
    await queue_manager.retry_failed()
    return await queue_manager.snapshot()


@app.post("/api/queue/clear-completed")
async def clear_completed() -> dict[str, Any]:
    await queue_manager.clear_completed()
    return await queue_manager.snapshot()


@app.post("/api/queue/clear-all")
async def clear_all(confirm: ConfirmRequest) -> dict[str, Any]:
    if not confirm.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    await queue_manager.clear_all()
    return await queue_manager.snapshot()


@app.post("/api/likes/start")
async def start_likes_sync() -> dict[str, Any]:
    item = await queue_manager.start_or_resume_likes_sync(retry_failed_only=False)
    return {"item": item.public(), "queue": await queue_manager.snapshot(), "stats": app_stats()}


@app.post("/api/likes/resume")
async def resume_likes_sync() -> dict[str, Any]:
    item = await queue_manager.start_or_resume_likes_sync(retry_failed_only=False)
    return {"item": item.public(), "queue": await queue_manager.snapshot(), "stats": app_stats()}


@app.post("/api/likes/retry-failed")
async def retry_failed_likes_sync() -> dict[str, Any]:
    item = await queue_manager.start_or_resume_likes_sync(retry_failed_only=True)
    return {"item": item.public(), "queue": await queue_manager.snapshot(), "stats": app_stats()}


@app.get("/api/history")
async def get_history(
    status: str = "All",
    search: str = "",
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    return history_query(status=status, search=search, page=page, page_size=page_size)


@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    return app_stats()


async def run_collect(command: list[str], masked_command: list[str], log_path: Path) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    token = get_auth_token()
    output: list[str] = ["Command: " + " ".join(masked_command), ""]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["XDG_CONFIG_HOME"] = str(CONFIG_DIR)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(DOWNLOAD_DIR),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            output.extend(mask_text(line.decode("utf-8", errors="replace"), token).replace("\r", "\n").splitlines())
        return_code = await process.wait()
    except Exception as exc:
        return_code = 1
        output.append(f"Failed to run scdl: {exc}")
    text = "\n".join(line for line in output if line is not None)
    log_path.write_text(text + "\n", encoding="utf-8")
    return return_code, text


@app.post("/api/qualities")
async def check_qualities(request: QualityRequest) -> dict[str, Any]:
    command, masked, _ = build_scdl_args("check-qualities", request.url, archive_enabled=False)
    log_path = LOG_DIR / f"quality-{uuid.uuid4().hex[:12]}.log"
    return_code, output = await run_collect(command, masked, log_path)
    parsed = parse_quality_output(output)
    return {
        "return_code": return_code,
        "command": masked,
        "log_path": str(log_path),
        **parsed,
    }


@app.get("/api/recent")
async def get_recent() -> dict[str, Any]:
    return {"download_dir": str(DOWNLOAD_DIR), "files": recent_files()}


def archive_count() -> int:
    ensure_directories()
    try:
        return sum(1 for line in ARCHIVE_PATH.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


@app.get("/api/archive")
async def get_archive() -> dict[str, Any]:
    return {"path": str(ARCHIVE_PATH), "count": archive_count(), "enabled": load_settings()["archive_enabled"]}


@app.post("/api/archive/clear")
async def clear_archive(confirm: ConfirmRequest) -> dict[str, Any]:
    if not confirm.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    ensure_directories()
    ARCHIVE_PATH.write_text("", encoding="utf-8")
    return {"path": str(ARCHIVE_PATH), "count": 0}


@app.get("/api/archive/export")
async def export_archive() -> FileResponse:
    ensure_directories()
    return FileResponse(ARCHIVE_PATH, filename="archive.txt", media_type="text/plain")


@app.post("/api/archive/import")
async def import_archive(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read(ARCHIVE_IMPORT_LIMIT + 1)
    if len(data) > ARCHIVE_IMPORT_LIMIT:
        raise HTTPException(status_code=400, detail="Archive file is too large")
    text = data.decode("utf-8", errors="ignore")
    clean_lines = [line.strip() for line in text.splitlines() if line.strip()]
    ARCHIVE_PATH.write_text("\n".join(clean_lines) + ("\n" if clean_lines else ""), encoding="utf-8")
    return {"path": str(ARCHIVE_PATH), "count": len(clean_lines)}


def writable_check(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_path = path / ".scdl-web-write-test"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink()
        return True, "writable"
    except OSError as exc:
        return False, str(exc)


def command_version(command: str) -> str:
    try:
        result = subprocess.run(
            [command, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
    except Exception:
        return "unavailable"


def health_payload() -> dict[str, Any]:
    ensure_directories()
    downloads_writable, downloads_message = writable_check(DOWNLOAD_DIR)
    config_writable, config_message = writable_check(CONFIG_DIR)
    logs_writable, logs_message = writable_check(LOG_DIR)
    archive_ok = False
    archive_message = "unknown"
    try:
        ARCHIVE_PATH.touch(exist_ok=True)
        archive_ok = ARCHIVE_PATH.exists()
        archive_message = "accessible"
    except OSError as exc:
        archive_message = str(exc)
    db_ok = False
    db_message = "unknown"
    try:
        init_db()
        with db_connect() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
        db_message = "accessible"
    except Exception as exc:
        db_message = str(exc)
    scdl_path = shutil.which(scdl_command())
    ffmpeg_path = shutil.which("ffmpeg")
    return {
        "app": {"ok": True, "version": APP_VERSION},
        "scdl": {
            "ok": bool(scdl_path),
            "command": scdl_command(),
            "path": scdl_path,
            "version": command_version(scdl_command()) if scdl_path else "unavailable",
        },
        "ffmpeg": {"ok": bool(ffmpeg_path), "path": ffmpeg_path},
        "downloads": {"ok": DOWNLOAD_DIR.exists() and downloads_writable, "path": str(DOWNLOAD_DIR), "message": downloads_message},
        "config": {"ok": CONFIG_DIR.exists() and config_writable, "path": str(CONFIG_DIR), "message": config_message},
        "archive": {"ok": archive_ok, "path": str(ARCHIVE_PATH), "message": archive_message, "count": archive_count()},
        "history": {"ok": db_ok, "path": str(DB_PATH), "message": db_message},
        "logs": {"ok": LOG_DIR.exists() and logs_writable, "path": str(LOG_DIR), "message": logs_message},
        "python": sys.version.split()[0],
    }


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return health_payload()


@app.get("/health")
async def health() -> dict[str, Any]:
    return health_payload()


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream():
        subscriber = await queue_manager.subscribe()
        try:
            yield f"data: {json.dumps({'type': 'snapshot', 'queue': await queue_manager.snapshot()})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(subscriber.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            queue_manager.unsubscribe(subscriber)

    return StreamingResponse(stream(), media_type="text/event-stream")

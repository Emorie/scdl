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
from urllib.parse import urlparse, urlunparse

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
TERMINAL_STATUSES = {"Done", "Failed", "Skipped", "Cancelled"}
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
    "max_concurrent_downloads": env_int("MAX_CONCURRENT_DOWNLOADS", 1),
    "download_delay_seconds": env_float("DOWNLOAD_DELAY_SECONDS", 2),
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
}


class QueueAddRequest(BaseModel):
    urls: str | list[str] = ""
    preset: str = "best-original"
    autostart: bool = False
    archive_enabled: Optional[bool] = None


class QualityRequest(BaseModel):
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
    max_concurrent_downloads: Optional[int] = None
    download_delay_seconds: Optional[float] = None
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
    logs: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "target": self.target,
            "target_url": self.target_url,
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
            "logs": self.logs[-200:],
            "log_path": str(self.log_path),
            "files": self.files,
            "summary": self.summary,
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
                last_error TEXT
            )
            """,
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_updated ON queue_items(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_likes ON queue_items(is_likes_sync, status)")
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


def row_to_item(row: sqlite3.Row) -> QueueItem:
    preset_id = row["preset_id"]
    target_url = row["target_url"] or row["target"]
    try:
        command, masked, archive_enabled = build_scdl_args(
            preset_id,
            target_url,
            archive_enabled=bool(row["archive_enabled"]),
        )
    except HTTPException:
        command, masked, archive_enabled = [], [], bool(row["archive_enabled"])
    return QueueItem(
        id=row["id"],
        preset_id=preset_id,
        preset_name=row["preset_name"],
        target=row["target"],
        target_url=target_url,
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
        log_path=Path(row["log_path"] or LOG_DIR / f"{row['id']}.log"),
        files=json_loads(row["files_json"], []),
        summary=json_loads(row["summary_json"], {}),
    )


def persist_item(item: QueueItem) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO queue_items (
                id, preset_id, preset_name, target, target_url, status, archive_enabled,
                is_likes_sync, created_at, updated_at, started_at, finished_at,
                return_code, log_path, files_json, summary_json, output_file,
                track_id, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                preset_id = excluded.preset_id,
                preset_name = excluded.preset_name,
                target = excluded.target,
                target_url = excluded.target_url,
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
                last_error = excluded.last_error
            """,
            (
                item.id,
                item.preset_id,
                item.preset_name,
                item.target,
                item.target_url,
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
            ),
        )
        conn.commit()


def load_active_items() -> list[QueueItem]:
    init_db()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM queue_items
            WHERE status IN ('Pending', 'Running')
            ORDER BY created_at ASC
            """,
        ).fetchall()
    return [row_to_item(row) for row in rows]


def history_status_filter(status: str) -> tuple[str, list[Any]]:
    normalized = status.strip().lower()
    if normalized in {"downloaded", "done"}:
        return "status = ?", ["Done"]
    if normalized in {"failed", "skipped", "pending", "running", "cancelled"}:
        return "status = ?", [normalized.title()]
    if normalized == "remaining":
        return "status IN ('Pending', 'Running')", []
    return "1 = 1", []


def history_query(status: str = "All", search: str = "", page: int = 1, page_size: int = 25) -> dict[str, Any]:
    init_db()
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    where, params = history_status_filter(status)
    if search.strip():
        where += " AND (target LIKE ? OR target_url LIKE ? OR preset_name LIKE ? OR output_file LIKE ? OR last_error LIKE ?)"
        needle = f"%{search.strip()}%"
        params.extend([needle, needle, needle, needle, needle])
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
        "summary": json_loads(row["summary_json"], {}),
    }


def app_stats() -> dict[str, Any]:
    init_db()
    with db_connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM queue_items GROUP BY status").fetchall()
        history_count = conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0]
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
            WHERE status = 'Failed'
            ORDER BY updated_at DESC
            LIMIT 5
            """,
        ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    downloaded = counts.get("Done", 0)
    failed = counts.get("Failed", 0)
    skipped = counts.get("Skipped", 0)
    pending = counts.get("Pending", 0)
    running = counts.get("Running", 0)
    return {
        "history_count": history_count,
        "archive_count": archive_count(),
        "total_processed": downloaded + failed + skipped,
        "downloaded": downloaded,
        "failed": failed,
        "skipped": skipped,
        "pending": pending,
        "running": running,
        "remaining_unknown": pending + running,
        "latest_likes_sync": history_row_public(likes) if likes else None,
        "recent_failures": [history_row_public(row) for row in recent_failures],
    }


def failed_likes_items() -> list[QueueItem]:
    init_db()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM queue_items
            WHERE is_likes_sync = 1 AND status = 'Failed'
            ORDER BY updated_at ASC
            """,
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
    try:
        settings["max_concurrent_downloads"] = max(1, int(settings.get("max_concurrent_downloads") or 1))
    except (TypeError, ValueError):
        settings["max_concurrent_downloads"] = 1
    try:
        settings["download_delay_seconds"] = max(0.0, float(settings.get("download_delay_seconds") or 0))
    except (TypeError, ValueError):
        settings["download_delay_seconds"] = 2
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
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")
    return clean[:80] or "soundcloud"


def artist_download_dir(target: str) -> Path:
    parsed = urlparse(target)
    artist = safe_path_component(parsed.path.strip("/").split("/", 1)[0])
    path = DOWNLOAD_DIR / artist
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


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
) -> tuple[list[str], list[str], bool]:
    settings = load_settings()
    preset = PRESETS.get(preset_id)
    if not preset:
        raise HTTPException(status_code=400, detail="Unknown preset")

    if preset.needs_url:
        target = validate_soundcloud_url(target)
    else:
        target = "me likes"
        if not get_auth_token(settings):
            raise HTTPException(status_code=400, detail="A SoundCloud auth token is required for My Likes Sync")

    args: list[str] = []
    for arg in preset.args:
        args.append(target if arg == "{url}" else arg)

    should_use_archive = settings.get("archive_enabled", True) if archive_enabled is None else archive_enabled
    if preset.id == "likes-best":
        should_use_archive = True

    if preset.downloads:
        target_download_dir = DOWNLOAD_DIR
        if settings.get("artist_folders") and preset.needs_url:
            target_download_dir = artist_download_dir(target)
        args.extend(["--path", str(target_download_dir)])
        if should_use_archive:
            args.extend(["--download-archive", str(ARCHIVE_PATH)])
        args.extend(["-c", "--retries", "3"])

        name_format = str(settings.get("name_format") or "").strip()
        if name_format:
            args.extend(["--name-format", name_format])

        playlist_format = str(settings.get("playlist_name_format") or "").strip()
        if playlist_format:
            args.extend(["--playlist-name-format", playlist_format])

        if settings.get("no_playlist_folder"):
            args.append("--no-playlist-folder")
        if settings.get("original_art"):
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
    return {
        "name": path.name,
        "extension": path.suffix.lower().lstrip(".") or "file",
        "size": stat.st_size,
        "size_label": human_size(stat.st_size),
        "modified": iso_time(stat.st_mtime),
        "folder": "." if folder == "." else folder,
        "path": relative,
    }


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
                command, masked, archive_enabled = build_scdl_args(
                    request.preset,
                    target,
                    archive_enabled=request.archive_enabled,
                )
                job_id = uuid.uuid4().hex[:12]
                is_likes_sync = preset.id == "likes-best"
                item = QueueItem(
                    id=job_id,
                    preset_id=preset.id,
                    preset_name=preset.name,
                    target=target if preset.needs_url else "My likes",
                    target_url=target if preset.needs_url else "me likes",
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
        clean = mask_text(text, token).replace("\r", "\n")
        lines = clean.splitlines() or [clean]
        with item.log_path.open("a", encoding="utf-8") as handle:
            for raw_line in lines:
                line = raw_line.rstrip()
                if not line:
                    continue
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
        item.updated_at = time.time()

    async def run_item(self, item: QueueItem) -> None:
        before = snapshot_files()
        item.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            command, masked, archive_enabled = build_scdl_args(
                item.preset_id,
                item.target_url,
                archive_enabled=item.archive_enabled,
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
                if item.summary.get("skipped"):
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
            persist_item(item)
            async with self.lock:
                if auth_error:
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
                    (candidate for candidate in self.items if candidate.id == item_id and candidate.status == "Pending"),
                    None,
                )
                if pending_item:
                    pending_item.status = "Cancelled"
                    pending_item.finished_at = time.time()
                    pending_item.updated_at = pending_item.finished_at
                    pending_item.last_error = "Cancelled before running"
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
            item.updated_at = time.time()
            persist_item(item)
        await self.broadcast({"type": "snapshot", "queue": await self.snapshot()})
        await self.kick()

    async def retry_failed(self) -> None:
        async with self.lock:
            for item in self.items:
                if item.status == "Failed":
                    item.status = "Pending"
                    item.return_code = None
                    item.started_at = None
                    item.finished_at = None
                    item.logs = []
                    item.files = []
                    item.summary = {}
                    item.last_error = None
                    item.output_file = None
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

        failed = failed_likes_items()
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
            selected.updated_at = time.time()
            persist_item(selected)
            async with self.lock:
                if not any(item.id == selected.id for item in self.items):
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
        elif key == "default_preset" and value in PRESETS:
            settings[key] = value
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

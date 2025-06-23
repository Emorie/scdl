#!/usr/bin/env python3
"""
sync_sc.py - tiny wrapper around `scdl` that keeps your SoundCloud playlists
mirrored locally. Add or remove playlist URLs in PLAYLISTS and point cron / Task
Scheduler at this file to automate.
"""

import pathlib
import subprocess
import sys

# Folder to keep your downloads
BASE = pathlib.Path.home() / "Music" / "SoundCloud"
# One archive file remembers every track that has already been downloaded
ARCHIVE = BASE / "archive.txt"

# \u270F\ufe0f  Add/Remove your playlist or like URLs here
PLAYLISTS = [
    "https://soundcloud.com/yourname/sets/my-playlist",
]

BASE.mkdir(parents=True, exist_ok=True)


def sync(url: str) -> None:
    subprocess.run(
        [
            "scdl",
            "-l",
            url,
            "--path",
            str(BASE),
            "--download-archive",
            str(ARCHIVE),
            "-c",
        ],
        check=True,
    )


def main() -> None:
    for url in PLAYLISTS:
        sync(url)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)

# Soundcloud Music Downloader
## Description

This script is able to download music from SoundCloud and set id3tag to the downloaded music.
Compatible with Windows, OS X, Linux.


## System requirements

* python3
* ffmpeg

## Installation Instructions
https://github.com/flyingrub/scdl/wiki/Installation-Instruction

## Configuration
There is a configuration file left in `~/.config/scdl/scdl.cfg`

## Examples:
```
# Download track & repost of the user QUANTA
scdl -l https://soundcloud.com/quanta-uk -a

# Download likes of the user Blastoyz
scdl -l https://soundcloud.com/kobiblastoyz -f

# Download one track
scdl -l https://soundcloud.com/jumpstreetpsy/low-extender

# Download one playlist
scdl -l https://soundcloud.com/pandadub/sets/the-lost-ship

# Download only new tracks from a playlist
scdl -l https://soundcloud.com/pandadub/sets/the-lost-ship --download-archive archive.txt -c

# Sync playlist
scdl -l https://soundcloud.com/pandadub/sets/the-lost-ship --sync archive.txt

# Download your likes (with authentification token)
scdl me -f
```

## Options:
```
-h --help                       Show this screen
--version                       Show version
-l [url]                        URL can be track/playlist/user
-s [search_query]               Search for a track/playlist/user and use the first result
-n [maxtracks]                  Download the n last tracks of a playlist according to the creation date
-a                              Download all tracks of user (including reposts)
-t                              Download all uploads of a user (no reposts)
-f                              Download all favorites (likes) of a user
-C                              Download all tracks commented on by a user
-p                              Download all playlists of a user
-r                              Download all reposts of user
-c                              Continue if a downloaded file already exists
--force-metadata                This will set metadata on already downloaded track
-o [offset]                     Start downloading a playlist from the [offset]th track (starting with 1)
--addtimestamp                  Add track creation timestamp to filename,
                                which allows for chronological sorting
                                (Deprecated. Use --name-format instead.)
--addtofile                     Add artist to filename if missing
--debug                         Set log level to DEBUG
--error                         Set log level to ERROR
--download-archive [file]       Keep track of track IDs in an archive file,
                                and skip already-downloaded files.
                                The archive is loaded once at startup for
                                faster duplicate checks
--extract-artist                Set artist tag from title instead of username
--hide-progress                 Hide the wget progress bar
--hidewarnings                  Hide Warnings. (use with precaution)
--max-size [max-size]           Skip tracks larger than size (k/m/g)
--min-size [min-size]           Skip tracks smaller than size (k/m/g)
--no-playlist-folder            Download playlist tracks into main directory,
                                instead of making a playlist subfolder
--onlymp3                       Download only mp3 files
--path [path]                   Use a custom path for downloaded files
--remove                        Remove any files not downloaded from execution
--sync [file]                   Compares an archive file to a playlist and downloads/removes any changed tracks
--flac                          Convert original files to .flac. Only works if the original file is lossless quality
--no-album-tag                  On some player track get the same cover art if from the same album, this prevent it
--original-art                  Download original cover art, not just 500x500 JPEG
--original-name                 Do not change name of original file downloads
--original-metadata             Do not change metadata of original file downloads
--no-original                   Do not download original file; only mp3, m4a, or opus
--only-original                 Only download songs with original file available
--name-format [format]          Specify the downloaded file name format. Use "-" to download to stdout
--playlist-name-format [format] Specify the downloaded file name format, if it is being downloaded as part of a playlist
--client-id [id]                Specify the client_id to use
--auth-token [token]            Specify the auth token to use
--overwrite                     Overwrite file if it already exists
--strict-playlist               Abort playlist downloading if one track fails to download
--add-description               Adds the description to a seperate txt file (can be read by some players)
--no-playlist                   Skip downloading playlists
--opus                          Prefer downloading opus streams over mp3 streams
--best-quality                  Try to download lossless audio first, converting to FLAC when
                                available, and fall back to the next best quality
--list-qualities                List available stream qualities for a track
--retries <retries>             Retry failed network requests up to <retries> times
```


## Features
* Automatically detect the type of link provided
* Download all songs from a user
* Download all songs and reposts from a user
* Download all songs from one playlist
* Download all songs from all playlists from a user
* Download all songs from a user's favorites
* Download only new tracks from a list (playlist, favorites, etc.)
* Sync Playlist
* Set the tags with mutagen (Title / Artist / Album / Artwork)
* Create playlist files when downloading a playlist

## Docker Web App

This fork includes a NAS-friendly FastAPI web wrapper for `scdl`. It is a UI and
queue around the existing CLI; it does not replace the downloader logic.

Default web downloads run:

```bash
scdl -l URL --best-quality --path /downloads --download-archive /config/archive.txt -c --retries 3
```

Metadata and organization settings may add approved `scdl` flags such as
`--name-format`, `--playlist-name-format`, `--original-metadata`, or
`--force-metadata`. The web app still builds commands from preset argument
arrays and never shells out to arbitrary browser input.

`Best Original Available` is the default preset. It uses this fork's
`--best-quality` behavior: try original/lossless first, convert original
lossless sources to FLAC when the CLI confirms that path is available, then fall
back to the best available stream. The web app does not transcode MP3 or Opus to
FLAC and call that lossless.

### Web Presets

| Preset | Command arguments |
| --- | --- |
| Best Original Available | `scdl -l URL --best-quality --path /downloads --download-archive /config/archive.txt -c --retries 3` |
| Original Only | `scdl -l URL --only-original --path /downloads --download-archive /config/archive.txt -c --retries 3` |
| Prefer Opus | `scdl -l URL --opus --path /downloads --download-archive /config/archive.txt -c --retries 3` |
| Stream Only / No Original | `scdl -l URL --no-original --path /downloads --download-archive /config/archive.txt -c --retries 3` |
| Check Qualities | `scdl -l URL --list-qualities` |
| My Likes Best Quality | `scdl me -f --best-quality --path /downloads --download-archive /config/archive.txt -c --retries 3` |
| Playlist Best Quality | `scdl -l URL --best-quality --path /downloads --download-archive /config/archive.txt -c --retries 3` |
| Metadata Repair / Force Metadata | `scdl -l URL --force-metadata --path /downloads --download-archive /config/archive.txt -c --retries 3` |

If a SoundCloud auth token is configured, the web app appends
`--auth-token TOKEN` to relevant `scdl` commands and masks it in browser logs.

### NAS Install

1. Clone this repo:

   ```bash
   git clone https://github.com/Emorie/scdl.git
   cd scdl
   ```

2. Open your Docker, UGREEN, Synology, or Portainer project editor.
3. Paste `docker-compose.yml`.
4. Adjust the volume paths if needed:
   - `/downloads` is where audio files are saved.
   - `/config` stores settings, archive, history, and logs.
5. Build or redeploy the stack.
6. Open:

   ```text
   http://NAS-IP:8090
   ```

7. Paste a SoundCloud URL.
8. Choose `Best Original Available`.
9. Click `Check Qualities` if desired.
10. Start the download.

### Docker Compose

```yaml
services:
  scdl-web:
    build: .
    container_name: scdl_web
    command:
      - uvicorn
      - scdl_web.main:app
      - --host
      - 0.0.0.0
      - --port
      - "8090"
    ports:
      - "8090:8090"
    environment:
      - PUID=1001
      - PGID=100
      - TZ=America/New_York
      - DOWNLOAD_DIR=/downloads
      - CONFIG_DIR=/config
      - DEFAULT_PRESET=best-original
      - SOUNDCLOUD_AUTH_TOKEN=
      - MAX_CONCURRENT_DOWNLOADS=1
      - DOWNLOAD_DELAY_SECONDS=2
    volumes:
      - /volume1/arr-stack-music/downloads/music/soundcloud:/downloads
      - /volume3/docker/music-stack/appdata/scdl:/config
    restart: unless-stopped
```

### Auth Token

Auth may be needed for likes, private or limited tracks, and some
original-quality downloads.

Set it one of two ways:

- In Docker Compose: `SOUNDCLOUD_AUTH_TOKEN=your-token`
- In the web UI settings panel. The value is saved to `/config/settings.json`.

Tokens are masked in browser logs and command displays.

### Metadata and Search Tags

SoundCloud metadata can be messy. The uploader is often not the real artist,
especially for remixes, edits, reposted DJ sets, and random profile uploads.
Tagged metadata or title patterns may contain a better artist candidate.

The web app preserves metadata in `/config/app.db` for search and history. For
each new downloaded audio file it records the output path, title, selected
artist, uploader, embedded/tagged artist, parsed artist candidate, genre, tags,
description, source URL, track ID when known, playlist/album context when
confident, artwork URL when available, date, downloaded time, and quality
summary.

The Metadata Settings card controls artist priority:

- `Smart Auto`: prefer embedded/tagged artist, then SoundCloud uploader, then
  a title parse candidate, then `Unknown Artist`.
- `Uploader First`: use the SoundCloud profile/uploader first.
- `Tagged Metadata First`: prefer embedded/tagged artist first.
- `Title Parse First`: try patterns such as `Artist - Track`, `Artist: Track`,
  `Song Title (Artist Remix)`, and `Song Title [Artist Edit]` before falling
  back.

By default, the app preserves original metadata, asks `scdl` to keep original
file metadata when possible, then fills missing tags after download with
Mutagen. `Force Metadata` is for intentional repair passes where you want more
aggressive filling/overwriting. Music apps vary in which fields they read, so
genre/tags/source URL may show differently depending on file type and player.

Optional sidecar JSON can be enabled in the UI. Sidecars are written next to
new audio files as `{filename}.ext.json` and are useful for future library
organizers or manual cleanup.

History search includes title, artist, uploader, tagged artist, parsed artist,
genre, tags, playlist, source URL, and track ID when those fields are known.

### Download Organization

The Organization card controls new downloads only. It does not move old files;
a future library organizer can handle that separately.

Modes:

- `Library Clean` is the default and recommended daily mode. Likes go under
  `Likes/{artist_or_uploader}`, playlists under
  `Playlists/{playlist_title}`, singles under `Artists/{artist_or_uploader}`,
  and uncertain metadata under `Unknown`.
- `Flat Downloads` puts everything directly in `/downloads` with clean
  `{artist_or_uploader} - {track_title}` filenames. Use it only for quick tests.
- `By Artist` puts downloads under `Artists/{artist_or_uploader}`.
- `By Playlist` keeps playlist downloads under `Playlists/{playlist_title}` and
  puts non-playlist tracks under `Singles`.
- `By Source Type` separates `Likes`, `Playlists`, `Singles`, and `Profiles`.
  This is a strong choice for very large likes libraries.
- `Original Structure / scdl Default` leaves `scdl`'s original folder behavior
  alone.

For 20k+ liked tracks, use `Library Clean` or `By Source Type`. The app uses
NAS-safe filenames, avoids illegal path characters, keeps paths bounded, and
adds the track ID or a short suffix if a filename collision occurs. You can
also enable track IDs or upload dates in filenames from the UI.

### Syncing My SoundCloud Likes

Use the `My Likes Sync` card when you want the app to resume through a large
liked-track library with the archive enabled.

1. Add your SoundCloud auth token in the Settings/Auth section, or set
   `SOUNDCLOUD_AUTH_TOKEN` in Docker Compose.
2. Click `Test Auth` to confirm the token works without downloading anything.
3. Click `Start / Resume Likes Sync`.

Likes Sync runs the same best-quality CLI path as manual downloads:

```bash
scdl me -f --best-quality --path /downloads --download-archive /config/archive.txt -c --retries 3
```

When a token is available, the app appends `--auth-token TOKEN` and masks it in
logs and API responses.

Resume uses two layers:

- `scdl` archive: `/config/archive.txt` is the source of truth for skipping
  tracks that were already downloaded.
- App history DB: `/config/app.db` stores queue status, failures, logs, and
  UI counters so failed jobs can be retried after a restart.

Keep both `/config` and `/downloads` mounted to persistent NAS folders. If the
container stops or the NAS reboots, start Likes Sync again; `scdl` reloads the
archive and skips completed tracks. Use `Retry Failed Only` for failed Likes
Sync jobs. For 20k+ likes, keep `MAX_CONCURRENT_DOWNLOADS=1`; the default
`DOWNLOAD_DELAY_SECONDS=2` is intentionally rate-limit friendly for normal queue
items.

### Paths

- Downloads: `/downloads`
- Settings: `/config/settings.json`
- Archive: `/config/archive.txt`
- History DB: `/config/app.db`
- Logs: `/config/logs`
- Optional sidecar JSON: next to downloaded audio files when enabled

### Updating on NAS

```bash
cd /volume3/docker/music-stack/apps/scdl-web
git pull
docker compose up -d --build
```

Shortcut from the repo folder:

```bash
bash scripts/update.sh
```

The expected NAS folder layout keeps app code, config, and downloads separate:

- App code: `/volume3/docker/music-stack/apps/scdl-web`
- Config: `/volume3/docker/music-stack/appdata/scdl`
- Downloads: `/volume1/arr-stack-music/downloads/music/soundcloud`

### Troubleshooting

- Open `/health` or the Health card in the web UI.
- If `scdl` is unavailable, rebuild the image so `pip install -e .` runs.
- If `ffmpeg` is unavailable, rebuild the Docker image; the Dockerfile installs
  it with apt.
- If `/downloads` or `/config` is not writable, check your mounted NAS paths and
  `PUID`/`PGID`.
- If likes or original downloads fail, add a valid SoundCloud auth token.
- If a download is skipped, check `/config/archive.txt` or temporarily disable
  archive for that queue item.

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
   - `/config` stores settings, archive, and logs.
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

### Paths

- Downloads: `/downloads`
- Settings: `/config/settings.json`
- Archive: `/config/archive.txt`
- Logs: `/config/logs`

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

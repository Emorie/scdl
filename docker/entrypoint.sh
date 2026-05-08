#!/bin/sh
set -eu

PUID="${PUID:-1001}"
PGID="${PGID:-100}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/downloads}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
TZ="${TZ:-America/New_York}"

mkdir -p "$DOWNLOAD_DIR" "$CONFIG_DIR" "$CONFIG_DIR/logs"
touch "$CONFIG_DIR/archive.txt"

if [ -f "/usr/share/zoneinfo/$TZ" ]; then
  ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
  echo "$TZ" >/etc/timezone
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
  groupadd -g "$PGID" scdl
fi

if ! id -u scdl >/dev/null 2>&1; then
  useradd -u "$PUID" -g "$PGID" -d "$CONFIG_DIR" -s /usr/sbin/nologin scdl
fi

chown "$PUID:$PGID" "$DOWNLOAD_DIR" "$CONFIG_DIR" "$CONFIG_DIR/logs" "$CONFIG_DIR/archive.txt" 2>/dev/null || true

if [ "$#" -eq 1 ]; then
  set -- $1
fi

exec gosu "$PUID:$PGID" "$@"

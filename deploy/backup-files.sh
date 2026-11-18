#!/usr/bin/env bash
# Optional weekly backup of persistent projects/library. DB backup remains backup.sh.
set -euo pipefail
cd /opt/vkino/deploy
destination=/opt/vkino-backups
mkdir -p "$destination"
chmod 700 "$destination"
file="$destination/vkino-files-$(date +%F).tar.gz"
trap 'rm -f "$file.tmp"' EXIT
docker compose exec -T bot python -c '
import os,sys,tarfile
os.chdir("/data")
with tarfile.open(fileobj=sys.stdout.buffer,mode="w|gz") as archive:
    for name in ("work","ready","library","voice_cache","spoken_cache.json"):
        if os.path.exists(name): archive.add(name,arcname=name)
' > "$file.tmp"
test -s "$file.tmp"
mv "$file.tmp" "$file"
find "$destination" -name 'vkino-files-*.tar.gz' -mtime +21 -delete
printf '%s\n' "$file"

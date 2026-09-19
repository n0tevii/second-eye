#!/bin/sh
# Install in second-eye/tools only after reviewing the live NAS Compose settings.
# Usage: nas_upgrade.sh check|upgrade ROOT compose.yml VERSION IMAGE@sha256:DIGEST
set -eu
umask 077
[ "$#" -eq 5 ] || { echo 'Expected: check|upgrade ROOT COMPOSE VERSION IMAGE_DIGEST' >&2; exit 2; }
mode=$1
root=$2
compose_file=$3
version=$4
image=$5
case "$mode" in check|upgrade) ;; *) exit 2;; esac
case "$root" in /*/second-eye) ;; *) echo 'Unexpected project directory' >&2; exit 2;; esac
case "$compose_file" in compose.yml|docker-compose.yml) ;; *) exit 2;; esac
printf '%s\n' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || exit 2
printf '%s\n' "$image" | grep -Eq '^ghcr.io/n0tevii/second-eye@sha256:[a-f0-9]{64}$' || exit 2
[ "$(cd "$root" && pwd -P)" = "$root" ] || { echo 'Project path must not be a symlink' >&2; exit 2; }
for path in "$root/data" "$root/.env" "$root/$compose_file" "$root/tools"; do
    [ -e "$path" ] && [ ! -L "$path" ] || { echo 'Missing or symlinked project path' >&2; exit 2; }
done
[ -f "$root/data/goodprice.db" ] || { echo 'Existing database required' >&2; exit 2; }
[ -f "$root/tools/compose.image.yml" ] && [ -f "$root/tools/upgrade_db.py" ] || exit 2
docker_bin=$(command -v docker) || { echo 'Docker CLI not in PATH' >&2; exit 2; }
"$docker_bin" compose version >/dev/null
compose() { "$docker_bin" compose --project-name second-eye --project-directory "$root" --file "$root/$compose_file" "$@" 2>/dev/null || { echo 'Compose command failed; inspect configuration locally without sharing credentials' >&2; return 1; }; }
[ "$(compose config --services)" = second-eye ] || { echo 'Only a single second-eye service is supported' >&2; exit 2; }
[ "$("$docker_bin" inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' second-eye)" = second-eye ] || exit 2
[ "$("$docker_bin" inspect --format '{{.State.Running}}' second-eye)" = true ] || { echo 'Existing container must be running' >&2; exit 2; }
old_image=$("$docker_bin" inspect --format '{{.Image}}' second-eye)
resolved_image=$(compose config --images)
[ "$("$docker_bin" image inspect --format '{{.Id}}' "$resolved_image")" = "$old_image" ] || { echo 'Current Compose does not describe the running image' >&2; exit 2; }
# Compose reads .env to resolve configuration; never print resolved credentials.
# Check mode does not pull images, stop containers or change project files.
echo "Project: $root; target: $version; image: $image"
[ "$mode" = upgrade ] || exit 0
lock="$root/.upgrade-lock"
mkdir "$lock" || { echo 'Another upgrade or unresolved upgrade lock exists' >&2; exit 1; }
phase=preparing
backup=''
keep_lock=false
finish() {
    code=$?
    trap - EXIT HUP INT TERM
    set +e
    if [ "$phase" = stopped ]; then
        "$docker_bin" start second-eye >/dev/null
        echo 'Backup did not complete; attempted to restart unchanged old container' >&2
    elif [ "$phase" = migrating ]; then
        echo 'Upgrade failed; restoring old configuration and stopped-data backup' >&2
        restored=true
        if "$docker_bin" inspect --format '{{.Id}}' second-eye >/dev/null 2>&1; then
            "$docker_bin" stop --time 60 second-eye >/dev/null || restored=false
        fi
        if [ "$restored" = true ]; then
            cp -p "$backup/compose.before.yml" "$root/$compose_file" &&
                cp -p "$backup/env.before" "$root/.env" &&
                mv "$root/data" "$backup/failed-data" || restored=false
        fi
        if [ "$restored" = true ]; then
            tar -xpf "$backup/data.tar" -C "$root" || restored=false
        fi
        if [ "$restored" = true ]; then
            compose up -d --no-build --pull never --force-recreate second-eye >/dev/null || restored=false
        fi
        if [ "$restored" = true ]; then
            echo "Old data and configuration restored; old runtime still requires verification. Backup: $backup" >&2
        else
            keep_lock=true
            echo "ROLLBACK INCOMPLETE. Keep the service stopped and inspect backup: $backup" >&2
        fi
    fi
    if [ "$keep_lock" = false ]; then rmdir "$lock" 2>/dev/null; fi
    exit "$code"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
# Pull and check before downtime. Every version resolves to an immutable digest.
"$docker_bin" pull "$image"
[ "$("$docker_bin" image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" = linux/amd64 ] || exit 1
[ "$("$docker_bin" image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$image")" = "$version" ] || exit 1
"$docker_bin" run --rm --network none --entrypoint python "$image" -c 'import sys,goodprice; assert goodprice.__version__ == sys.argv[1].removeprefix("v")' "$version"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$root/backups/$stamp-before-$version"
[ ! -L "$root/backups" ] || exit 1
mkdir -p "$root/backups"
chmod 700 "$root/backups"
mkdir "$backup"
cp -p "$root/$compose_file" "$backup/compose.before.yml"
cp -p "$root/.env" "$backup/env.before"
printf '%s\n' "$old_image" > "$backup/old-image-id.txt"
sed "s|__SECOND_EYE_IMAGE__|$image|" "$root/tools/compose.image.yml" > "$backup/compose.after.yml"
"$docker_bin" compose --project-name second-eye --project-directory "$root" --file "$backup/compose.after.yml" config --quiet 2>/dev/null || { echo 'New Compose configuration is invalid' >&2; exit 1; }
phase=stopped
"$docker_bin" stop --time 60 second-eye >/dev/null
tar -cpf "$backup/data.tar" -C "$root" data
tar -tf "$backup/data.tar" >/dev/null
phase=migrating
# Pause before the new application starts: migration and startup checks cannot trigger crawls.
"$docker_bin" run --rm -i --network none --entrypoint python -v "$root/data:/app/data" "$image" - pause < "$root/tools/upgrade_db.py" > "$backup/enabled-tasks.json"
cp "$backup/compose.after.yml" "$root/$compose_file"
compose up -d --no-build --pull never --force-recreate second-eye
healthy=false
attempt=0
while [ "$attempt" -lt 60 ]; do
    if "$docker_bin" exec second-eye python -c 'import json,sys,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8000/healthz",timeout=3)); assert d["status"]=="ok" and d["version"]==sys.argv[1]' "$version" >/dev/null 2>&1; then
        healthy=true
        break
    fi
    attempt=$((attempt + 1))
    sleep 2
done
[ "$healthy" = true ] || { echo 'New version failed health/version checks' >&2; exit 1; }
[ "$("$docker_bin" inspect --format '{{.Image}}' second-eye)" = "$("$docker_bin" image inspect --format '{{.Id}}' "$image")" ] || exit 1
# Restore only the previously enabled tasks, after the version is verified.
"$docker_bin" exec -i second-eye python - resume "$(cat "$backup/enabled-tasks.json")" < "$root/tools/upgrade_db.py"
phase=complete
echo "UPGRADED $version. Backup: $backup. Real search and phone notification acceptance remain NOT_VALIDATED."

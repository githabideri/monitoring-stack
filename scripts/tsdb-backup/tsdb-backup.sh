#!/usr/bin/env bash
# tsdb-backup.sh — snapshot the Prometheus TSDB and copy it to a backup
# destination (e.g. an NAS dataset over NFS), then prune old copies.
#
# Designed to run on the PROXMOX HOST (or wherever the TSDB ZFS dataset is
# mounted), NOT inside the unprivileged Prometheus LXC (which cannot mount
# NFS). The host sees the same ZFS dataset the LXC uses.
#
# The backup destination must be reachable when this runs. If it is a
# power-saved NAS, schedule the timer inside its availability window and
# let failures fail loudly (journal); the local TSDB is never modified.
#
# Env / arguments (env wins if both given):
#   PROM_URL   Prometheus base URL          (default http://127.0.0.1:9090)
#   TSDB_DIR   Host path of the TSDB dataset (required)
#   DEST       Backup destination dir       (required; must be a mountpoint)
#   KEEP       Snapshots to keep in DEST    (default 14)
#   LOCAL_KEEP_DAYS  Age limit for local snapshot cleanup (default 2)
#   TSDB_BACKUP_TEST_HOOK  (test-only) shell hook run after the copy, before
#                 destination verification; empty in production.
#
# Exit codes: 0 ok, 1 config error, 2 snapshot failed, 3 copy or verification
# failed, 4 prune failed.
#
# Guarantee: the local snapshot is deleted only AFTER the destination copy has
# been verified (block dirs with meta.json/index/chunks, file count and size
# match the source). Any failed run keeps the local snapshot for the next
# attempt and exits non-zero (visible in the journal).

set -euo pipefail
# Be cwd-independent: some callers (ssh from /root, agents) leave us in a
# directory the service user cannot re-enter, which makes find(1) exit
# non-zero after it chdirs ("Failed to restore initial working directory").
cd /

PROM_URL="${PROM_URL:-${1:-http://127.0.0.1:9090}}"
TSDB_DIR="${TSDB_DIR:-${2:-}}"
DEST="${DEST:-${3:-}}"
KEEP="${KEEP:-14}"

log() { echo "$(date -Is) $*"; }

[ -n "$TSDB_DIR" ] || { log "ERROR: TSDB_DIR not set (host path of the TSDB dataset)"; exit 1; }
[ -n "$DEST" ]     || { log "ERROR: DEST not set (backup destination dir)"; exit 1; }
[ -d "$TSDB_DIR" ] || { log "ERROR: TSDB_DIR $TSDB_DIR not found"; exit 1; }
# -q: the path itself must be a mountpoint. Plain `mountpoint` also matches
# any path *inside* a mounted filesystem (e.g. under a tmpfs /tmp), which
# would let a plain directory pass the "NFS target" check.
mountpoint -q "$DEST" 2>/dev/null || { log "ERROR: DEST $DEST is not a mountpoint (NFS target down?)"; exit 1; }
[ -w "$DEST" ] || { log "ERROR: DEST $DEST not writable"; exit 1; }

# 1. Ask Prometheus to snapshot the live TSDB.
#    2.x: {"status":"success","data":{"filename":"..."}},
#    3.x: {"status":"success","data":{"name":"..."}} (key renamed).
#    The grep tolerates optional whitespace after the colon; the sed extracts
#    the last quoted string of the match.
log "requesting snapshot from $PROM_URL"
snap_json="$(curl -fsS -X POST "$PROM_URL/api/v1/admin/tsdb/snapshot")" || { log "ERROR: snapshot request failed"; exit 2; }
fname="$(echo "$snap_json" | grep -oE '"(filename|name)"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 | sed -E 's/.*"([^"]+)"$/\1/')"
[ -n "$fname" ] || { log "ERROR: no filename in response: $snap_json"; exit 2; }

src="$TSDB_DIR/snapshots/$fname"
[ -d "$src" ] || { log "ERROR: snapshot dir $src not found"; exit 2; }

# 2. Copy snapshot (rsync: resumable, near-atomic target dir).
#    Sequential checks on purpose — a grouped `if ! a && b` would skip `b`
#    entirely when `a` succeeds, which is exactly the silent-failure we must
#    never have here.
dst="$DEST/snapshots/$fname"
log "copying $src -> $dst"
if ! mkdir -p "$dst"; then
  log "ERROR: could not create destination $dst"
  exit 3
fi
if ! rsync -a --info=progress2 "$src/" "$dst/"; then
  log "ERROR: copy failed; cleaning partial target"
  rm -rf -- "$dst"
  exit 3
fi
# Test hook: run after the copy, before verification (empty in production).
# Lets the test suite simulate a corrupted/incomplete destination.
# (if/then on purpose: a trailing `false && ...` would trip set -e when the
# hook is unset.)
if [ -n "${TSDB_BACKUP_TEST_HOOK:-}" ]; then
  eval "$TSDB_BACKUP_TEST_HOOK"
fi

# 3. Verify the destination copy before touching anything local.
#    A Prometheus 3.x snapshot is a static copy of the TSDB: one or more
#    block dirs (beefy ids), each containing meta.json + index + chunks/.
#    (Prometheus 2.x used index/ + chunks/ directly under the snapshot dir.
#    The file-count equality check below covers both layouts.)
log "verifying destination copy"
src_files=$(find "$src" -type f | wc -l)
dst_files=$(find "$dst" -type f | wc -l)
if [ "$src_files" -eq 0 ] || [ "$src_files" != "$dst_files" ]; then
  log "ERROR: verification failed: file count mismatch (src=$src_files dst=$dst_files)"
  rm -rf -- "$dst"
  exit 3
fi
for part in "$dst"/*; do
  [ -d "$part" ] || continue
  for need in "meta.json" "index" "chunks"; do
    if [ ! -e "$part/$need" ]; then
      log "ERROR: verification failed: $part/$need missing"
      rm -rf -- "$dst"
      exit 3
    fi
  done
done
src_bytes=$(du -sb "$src" | cut -f1)
dst_bytes=$(du -sb "$dst" | cut -f1)
# a truncated transfer keeps the file count but loses bytes; require >=90%
if [ "$dst_bytes" -lt $((src_bytes * 9 / 10)) ]; then
  log "ERROR: verification failed: size mismatch (src=$src_bytes dst=$dst_bytes)"
  rm -rf -- "$dst"
  exit 3
fi
log "verified: $dst ($src_files files, $dst_bytes bytes)"

# 4. Only now delete this run's local snapshot. A snapshot dir is a static
#    copy of the live TSDB — deleting it never touches the live data. Also
#    remove anything older than LOCAL_KEEP_DAYS (default 2) left by earlier
#    failed runs (3.x has no delete/list snapshot API, so this is how they go
#    away).
log "pruning local snapshots (keep ${LOCAL_KEEP_DAYS:-2}d)"
find "$TSDB_DIR/snapshots" -mindepth 1 -maxdepth 1 -type d \
  -mtime +"${LOCAL_KEEP_DAYS:-2}" -exec rm -rf -- {} + 2>/dev/null || true
rm -rf -- "$src"

# 5. Prune: keep the newest KEEP snapshot dirs in DEST.
log "pruning DEST to newest $KEEP"
(
  cd "$DEST/snapshots"
  ls -1 | sort -r | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -rf -- "$old"
  done
) || { log "ERROR: prune failed"; exit 4; }

log "done: $fname ($(du -sh "$dst" | cut -f1))"

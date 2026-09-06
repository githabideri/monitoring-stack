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
#
# Exit codes: 0 ok, 1 config error, 2 snapshot failed, 3 copy failed, 4 prune failed

set -euo pipefail

PROM_URL="${PROM_URL:-${1:-http://127.0.0.1:9090}}"
TSDB_DIR="${TSDB_DIR:-${2:-}}"
DEST="${DEST:-${3:-}}"
KEEP="${KEEP:-14}"

log() { echo "$(date -Is) $*"; }

[ -n "$TSDB_DIR" ] || { log "ERROR: TSDB_DIR not set (host path of the TSDB dataset)"; exit 1; }
[ -n "$DEST" ]     || { log "ERROR: DEST not set (backup destination dir)"; exit 1; }
[ -d "$TSDB_DIR" ] || { log "ERROR: TSDB_DIR $TSDB_DIR not found"; exit 1; }
[ -n "$(mountpoint "$DEST" 2>/dev/null)" ] || { log "ERROR: DEST $DEST is not a mountpoint (NFS target down?)"; exit 1; }
[ -w "$DEST" ] || { log "ERROR: DEST $DEST not writable"; exit 1; }

# 1. Ask Prometheus to snapshot the live TSDB.
#    Response: {"status":"success","data":{"filename":"2026-09-06T...Z"}}
log "requesting snapshot from $PROM_URL"
snap_json="$(curl -fsS -X POST "$PROM_URL/api/v1/admin/tsdb/snapshot")" || { log "ERROR: snapshot request failed"; exit 2; }
fname="$(echo "$snap_json" | grep -oE '"filename":"[^"]+"' | cut -d'"' -f4)"
[ -n "$fname" ] || { log "ERROR: no filename in response: $snap_json"; exit 2; }

src="$TSDB_DIR/snapshots/$fname"
[ -d "$src" ] || { log "ERROR: snapshot dir $src not found"; exit 2; }

# 2. Copy snapshot (rsync: resumable, near-atomic target dir).
dst="$DEST/snapshots/$fname"
log "copying $src -> $dst"
if ! mkdir -p "$dst" && rsync -a --info=progress2 "$src/" "$dst/"; then
  log "ERROR: copy failed; cleaning partial target"
  rm -rf "$dst"
  exit 3
fi

# 3. Prune: keep the newest KEEP snapshot dirs in DEST.
log "pruning DEST to newest $KEEP"
(
  cd "$DEST/snapshots"
  ls -1 | sort -r | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -rf -- "$old"
  done
) || { log "ERROR: prune failed"; exit 4; }

log "done: $fname ($(du -sh "$dst" | cut -f1))"

#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Refresh the grype + trivy vulnerability databases, then delete the snapshots
# the refresh superseded, and report the resulting cache footprint.
#
# WHY THIS EXISTS:
#   On 2026-08-20 the Mac hit a 95% disk alert. ~/Library/Caches/grype was
#   21 GB and ~/Library/Caches/trivy was 2.6 GB — 23.6 GB, over half of
#   ~/Library/Caches. Both are the scanners behind the nightly surveillance
#   lane, which had been invoking them ad hoc with no prune step.
#
#   The two scanners do NOT leak the same way, and the difference decides the
#   fix:
#
#   * grype downloads into a staging directory named `db/grype-db-download<random>`
#     and moves it onto `db/6` on success. A run killed mid-download (cron
#     timeout, laptop sleep, SIGKILL) leaves that staging directory behind at
#     up to ~1.9 GB. The random suffix means it is never reused and never
#     overwritten, so every interrupted run adds another full copy. 21 GB is
#     roughly eleven of them. THIS is the leak.
#   * trivy writes `db/trivy.db` in place with no staging directory. Its
#     steady state is ~1.2 GB (plus java-db when used). It does not duplicate
#     its vulnerability DB, so there is no old snapshot to delete — only the
#     per-scan `--scan-cache`, which does accumulate.
#
#   `grype db delete` is therefore the WRONG tool: it removes the live DB and
#   forces a full ~90s re-download on the next run. We keep the live DB and
#   delete only what it superseded.
#
# THE PRUNE RULE:
#   Ask grype which database file it considers live (`grype db status -o json`
#   → .path), keep that directory, and delete every sibling under the cache
#   root. This collapses "orphaned staging dirs" and "stale schema dirs" into
#   one rule, and stays correct if the on-disk layout changes in a future
#   grype release, because grype itself names the survivor.
#
# ORDERING GUARANTEE:
#   Pruning runs ONLY after the update reports success AND grype reports a
#   valid database. A failed download must never leave the lane with no DB.
set -euo pipefail

# --- configuration ------------------------------------------------------

# An entry with ANY file modified inside it more recently than this may belong
# to a running download, so we leave it alone. Interrupted leftovers go
# untouched from the moment the run dies.
#
# This measures the newest mtime anywhere in the subtree, NOT the directory's
# own mtime. A directory's mtime only changes when entries are added or
# removed, so it stops advancing while a multi-GB file inside it is still being
# written — a download running longer than this window would have looked
# abandoned and been deleted mid-write.
STAGING_MIN_AGE_MIN="${NE_SCAN_STAGING_MIN_AGE_MIN:-60}"

# Post-prune the footprint should be ~3.2 GB (grype 1.9 + trivy 1.2). This
# ceiling is the regression tripwire, deliberately set well clear of normal
# growth so it means "something is wrong", not "the DBs grew a bit".
MAX_BYTES="${NE_SCAN_CACHE_MAX_BYTES:-8589934592}"  # 8 GiB

# An EXPLICITLY configured cache directory is used exactly as given, even if it
# does not exist yet. The platform fallback applies only when no variable was
# supplied. Rewriting a configured-but-not-yet-created path would point the
# scanner at one cache (it honours the exported variable and creates it) while
# pruning and footprint reporting operated on another — and every aged entry in
# that unrelated cache would then look superseded.
if [ -n "${GRYPE_DB_CACHE_DIR:-}" ]; then
  GRYPE_CACHE="$GRYPE_DB_CACHE_DIR"
else
  GRYPE_CACHE="${HOME}/Library/Caches/grype"
  [ -d "$GRYPE_CACHE" ] || GRYPE_CACHE="${HOME}/.cache/grype"
fi
if [ -n "${TRIVY_CACHE_DIR:-}" ]; then
  TRIVY_CACHE="$TRIVY_CACHE_DIR"
else
  TRIVY_CACHE="${HOME}/Library/Caches/trivy"
  [ -d "$TRIVY_CACHE" ] || TRIVY_CACHE="${HOME}/.cache/trivy"
fi

PRUNE_ONLY=0
ENFORCE=0
for arg in "$@"; do
  case "$arg" in
    --prune-only) PRUNE_ONLY=1 ;;
    --enforce)    ENFORCE=1 ;;
    -h|--help)
      sed -n '5,40p' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *)
      echo "ERROR: unknown argument '$arg'" >&2
      exit 2 ;;
  esac
done

log() { echo "[scan-db-refresh] $*"; }

# Size of a directory in bytes, 0 when absent. `du -sk` is the portable
# intersection of BSD and GNU du; multiply back up to bytes.
dir_bytes() {
  local d="$1"
  [ -d "$d" ] || { echo 0; return; }
  echo $(( $(du -sk "$d" 2>/dev/null | awk '{print $1}') * 1024 ))
}

human() { awk -v b="$1" 'BEGIN{ s="B KB MB GB TB"; split(s,u," ");
  for(i=1;b>=1024&&i<5;i++) b/=1024; printf "%.1f%s", b, u[i] }'; }

# --- refresh ------------------------------------------------------------

grype_ok=0
trivy_ok=0

if [ "$PRUNE_ONLY" = "1" ]; then
  log "prune-only mode: skipping database download"
  grype_ok=1
  trivy_ok=1
else
  if command -v grype >/dev/null 2>&1; then
    log "updating grype database..."
    if grype db update; then grype_ok=1; else
      log "WARNING: grype db update FAILED — keeping the existing database untouched"
    fi
  else
    log "grype not installed — skipping"
  fi

  if command -v trivy >/dev/null 2>&1; then
    log "updating trivy database..."
    if trivy fs --download-db-only --quiet; then trivy_ok=1; else
      log "WARNING: trivy db update FAILED — keeping the existing database untouched"
    fi
  else
    log "trivy not installed — skipping"
  fi
fi

# --- prune (only after a successful, verified update) -------------------

pruned_count=0
pruned_bytes=0

prune_grype() {
  local root="${GRYPE_CACHE}/db"
  [ -d "$root" ] || return 0

  # Ask grype which file is live. If it cannot tell us, or calls the DB
  # invalid, we delete NOTHING — an unreadable status is exactly the case
  # where a wrong guess destroys the working database.
  local status_json live_path live_dir
  if ! status_json="$(grype db status -o json 2>/dev/null)"; then
    log "WARNING: 'grype db status' failed — skipping grype prune (fail-safe)"
    return 0
  fi
  if ! echo "$status_json" | grep -q '"valid"[[:space:]]*:[[:space:]]*true'; then
    log "WARNING: grype reports the database is NOT valid — skipping prune (fail-safe)"
    return 0
  fi
  live_path="$(echo "$status_json" | sed -n 's/.*"path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
  if [ -z "$live_path" ] || [ ! -f "$live_path" ]; then
    log "WARNING: could not resolve the live grype DB path — skipping prune (fail-safe)"
    return 0
  fi
  local resolved_root resolved_live
  resolved_root="$(cd "$root" && pwd -P)"
  resolved_live="$(cd "$(dirname "$live_path")" && pwd -P)/$(basename "$live_path")"

  # The live DB must be inside the root we are about to prune. If it is not,
  # the configured cache and the tree we can see have diverged, and deleting
  # anything here would be deleting a cache we know nothing about.
  case "$resolved_live" in
    "$resolved_root"/*) : ;;
    *)
      log "WARNING: live DB ($resolved_live) is not inside the cache root ($resolved_root)"
      log "         — skipping grype prune (fail-safe)"
      return 0 ;;
  esac

  # Preserve the FIRST-LEVEL entry under the root that contains the live DB,
  # which is what the delete loop below compares against. Using the live file's
  # parent directory would be wrong for a layout that stores the DB directly at
  # `db/vulnerability.db`: the parent would equal the root itself, no first-level
  # entry would ever match it, and the live file would be deleted as a sibling.
  local rel keep
  rel="${resolved_live#"$resolved_root"/}"
  keep="${resolved_root}/${rel%%/*}"
  log "grype live database: ${resolved_live}"
  log "  preserving: ${keep}"

  # Everything else at the first level is superseded: an orphaned staging dir
  # from an interrupted download, or a stale schema dir from an older grype.
  local entry entry_abs sz
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    entry_abs="$(cd "$(dirname "$entry")" && pwd -P)/$(basename "$entry")"
    [ "$entry_abs" = "$keep" ] && continue

    # Leave anything with recent write activity anywhere inside it: that is a
    # download still in flight, not an abandoned one.
    if [ -n "$(find "$entry_abs" -mmin "-${STAGING_MIN_AGE_MIN}" -print -quit 2>/dev/null)" ]; then
      log "  in flight, leaving alone: $(basename "$entry_abs")"
      continue
    fi

    # Refuse to delete anything that is not strictly inside the cache root.
    case "$entry_abs" in
      "$resolved_root"/*) : ;;
      *) log "REFUSING to delete outside the cache root: $entry_abs"; continue ;;
    esac

    sz="$(dir_bytes "$entry_abs")"
    log "  removing superseded: $(basename "$entry_abs") ($(human "$sz"))"
    rm -rf "$entry_abs"
    pruned_count=$(( pruned_count + 1 ))
    pruned_bytes=$(( pruned_bytes + sz ))
    # Enumerate every first-level entry. The in-flight test above is what
    # protects a running download; it inspects the whole subtree, which a
    # `-mmin` filter on the entry itself cannot do.
  done < <(find "$resolved_root" -mindepth 1 -maxdepth 1 2>/dev/null)
}

prune_trivy() {
  command -v trivy >/dev/null 2>&1 || return 0
  # ONLY the per-scan cache. Never --vuln-db / --all: trivy replaces its
  # vulnerability DB in place, so deleting it just forces a re-download.
  local before after
  before="$(dir_bytes "$TRIVY_CACHE")"
  if trivy clean --scan-cache --quiet 2>/dev/null; then
    after="$(dir_bytes "$TRIVY_CACHE")"
    if [ "$before" -gt "$after" ]; then
      log "  trivy scan cache reclaimed: $(human $(( before - after )))"
      pruned_bytes=$(( pruned_bytes + before - after ))
    fi
  fi
}

if [ "$grype_ok" = "1" ]; then prune_grype; else
  log "skipping grype prune — the update did not succeed"
fi
if [ "$trivy_ok" = "1" ]; then prune_trivy; else
  log "skipping trivy prune — the update did not succeed"
fi

# --- report -------------------------------------------------------------

g_bytes="$(dir_bytes "$GRYPE_CACHE")"
t_bytes="$(dir_bytes "$TRIVY_CACHE")"
total=$(( g_bytes + t_bytes ))

log "reclaimed ${pruned_count} superseded snapshot(s), $(human "$pruned_bytes")"
# Machine-greppable footprint line — this is the per-run visibility that makes
# a silent regression impossible to miss.
echo "SCAN_CACHE_BYTES grype=${g_bytes} trivy=${t_bytes} total=${total} pruned=${pruned_bytes} limit=${MAX_BYTES}"
log "footprint: grype $(human "$g_bytes") + trivy $(human "$t_bytes") = $(human "$total")"

if [ "$total" -gt "$MAX_BYTES" ]; then
  echo "SCAN_CACHE_OVER_BUDGET total=${total} limit=${MAX_BYTES}" >&2
  log "WARNING: scanner caches exceed $(human "$MAX_BYTES") after pruning — investigate."
  # Exit non-zero only when explicitly asked. The nightly security lane calls
  # this without --enforce so that a disk-space anomaly can never suppress a
  # vulnerability scan.
  [ "$ENFORCE" = "1" ] && exit 3
fi
exit 0

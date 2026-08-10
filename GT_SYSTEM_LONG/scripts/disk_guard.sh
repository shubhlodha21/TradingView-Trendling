#!/usr/bin/env bash
# disk_guard.sh — keeps disk usage bounded during long-duration chaos runs.
#
# Designed to run from cron every 15-30 min. Safe to run while chaos test
# is active — only truncates/deletes files that the test does not need
# for its current operation.
#
# What it does (in order):
#   1. Truncates per-bot tmux stdout logs over 50 MB (in-place, append-safe)
#   2. Deletes feed.csv files over 10 MB (tick-level data, not needed by chaos test)
#   3. Compresses chaos_loop iter logs older than 6 hours (kept for reports)
#   4. Tar+gz's audit directories from yesterday or earlier, then deletes
#   5. Trims docker compose logs if Docker is running (size cap)
#   6. Reports new disk usage
#
# Install in cron:
#   crontab -e
#   */30 * * * * /home/ubuntu/GT_SYSTEM_TESTING/scripts/disk_guard.sh >> /home/ubuntu/GT_SYSTEM_TESTING/logs/disk_guard.log 2>&1

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

TS="$(date +%Y-%m-%dT%H:%M:%S)"
echo ""
echo "════════════════════════════════════════════════════════"
echo "[$TS] DISK_GUARD start  ($PROJECT_ROOT)"

# ── 0. Baseline disk + audit footprint ─────────────────────────────────
df_before=$(df -h / | tail -1 | awk '{print $4}')
echo "  free before: $df_before"

# ── 1. Truncate big per-bot tmux logs (append-only, safe to truncate) ──
n=0; saved=0
while IFS= read -r f; do
    sz_before=$(stat -c%s "$f" 2>/dev/null || echo 0)
    truncate -s 0 "$f" 2>/dev/null && { n=$((n+1)); saved=$((saved + sz_before)); }
done < <(find tests/paper/logs -type f -name "*.log" -size +50M 2>/dev/null)
echo "  truncated $n per-bot logs (~$((saved / 1024 / 1024)) MB freed)"

# ── 2. Delete large feed.csv files (per-tick data, not used by chaos test) ─
n=0; saved=0
while IFS= read -r f; do
    sz_before=$(stat -c%s "$f" 2>/dev/null || echo 0)
    rm -f "$f" && { n=$((n+1)); saved=$((saved + sz_before)); }
done < <(find data/audit -type f -name "feed.csv" -size +10M 2>/dev/null)
echo "  deleted $n feed.csv files (~$((saved / 1024 / 1024)) MB freed)"

# ── 3. Compress old chaos iter logs (kept for report generator) ────────
n=0
while IFS= read -r f; do
    gzip -9 "$f" 2>/dev/null && n=$((n+1))
done < <(find logs/chaos_loop logs/chaos_loop_equity -type f \
              -name "iter*.log" -mmin +360 ! -name "*.gz" 2>/dev/null)
echo "  gzipped $n chaos iter logs (>6h old)"

# ── 4. Archive + delete audit dirs from before today ───────────────────
TODAY=$(date +%Y%m%d)
n=0
while IFS= read -r d; do
    name=$(basename "$d")
    if [[ "$name" != "$TODAY" && "$name" =~ ^20[0-9]{6}$ ]]; then
        archive="data/audit/${name}.tar.gz"
        if [[ ! -f "$archive" ]]; then
            tar czf "$archive" -C data/audit "$name" 2>/dev/null && \
                rm -rf "$d" && n=$((n+1))
        else
            # archive exists, just remove the directory
            rm -rf "$d" && n=$((n+1))
        fi
    fi
done < <(find data/audit -maxdepth 1 -mindepth 1 -type d 2>/dev/null)
echo "  archived+removed $n prior-day audit dirs"

# ── 5. Trim Docker logs if Docker is present ───────────────────────────
if command -v docker >/dev/null 2>&1; then
    if docker ps -q 2>/dev/null | head -1 | grep -q .; then
        # `docker system prune --volumes` would also work, but is more aggressive.
        # This keeps containers + images, only clears stopped state.
        docker container prune -f >/dev/null 2>&1 && echo "  docker: pruned stopped containers"
    fi
fi

# ── 6. Compress the disk_guard log itself if huge ──────────────────────
if [[ -f logs/disk_guard.log ]]; then
    sz=$(stat -c%s logs/disk_guard.log 2>/dev/null || echo 0)
    if [[ $sz -gt 5242880 ]]; then  # 5 MB
        mv logs/disk_guard.log "logs/disk_guard.log.$(date +%Y%m%d_%H%M)"
        gzip "logs/disk_guard.log."*
        echo "  rotated disk_guard.log"
    fi
fi

# ── 7. Report ──────────────────────────────────────────────────────────
df_after=$(df -h / | tail -1 | awk '{print $4}')
echo "  free after : $df_after"
echo "[$TS] DISK_GUARD done"

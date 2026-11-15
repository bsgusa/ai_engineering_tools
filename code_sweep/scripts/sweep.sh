#!/usr/bin/env bash
# sweep.sh — deterministic staff-engineer check runner for the code_sweep engine.
#
# Runs, in order, every check a repo actually declares: lint, typecheck, test,
# check:claims (blocking), then tsc-advisory, npm audit, npm outdated (report only).
# No LLM, no writes to the repo. Outputs:
#   $SWEEP_OUT/results.txt   machine lines  <name>=pass|FAIL|absent|...
#   $SWEEP_OUT/summary.md    markdown for $GITHUB_STEP_SUMMARY / issues / PR bodies
#
# Exit: 0 = every blocking check that exists passed; 1 = at least one failed.
#
# Env:
#   SWEEP_OUT          output dir (default ./sweep)
#   SWEEP_SKIP_INSTALL =1 to reuse existing node_modules (local runs)
#   SWEEP_SKIP_AUDIT   =1 to skip registry calls (offline runs)

set -uo pipefail

OUT="${SWEEP_OUT:-./sweep}"
mkdir -p "$OUT"
RESULTS="$OUT/results.txt"
SUMMARY="$OUT/summary.md"
: > "$RESULTS"

fail=0
has_lock=0
[ -f package-lock.json ] && has_lock=1
[ -f pnpm-lock.yaml ] && has_lock=2

log()  { printf '%s\n' "$*" | tee -a "$RESULTS"; }
note() { printf '%s\n' "$*" >> "$RESULTS"; }

# ---------------------------------------------------------------- install ---
install_failed=0
if [ "${SWEEP_SKIP_INSTALL:-0}" != "1" ]; then
  case "$has_lock" in
    1) npm ci --no-audit --no-fund >"$OUT/install.log" 2>&1 || install_failed=1 ;;
    2) pnpm install --frozen-lockfile >"$OUT/install.log" 2>&1 || install_failed=1 ;;
    *) log "install=absent" ;;
  esac
  if [ "$install_failed" = 1 ]; then
    log "install=FAIL"
    {
      echo "# staff-sweep results — $(date -u '+%Y-%m-%d %H:%M UTC')"
      echo
      echo "**Install failed** — no checks were run. Tail:"
      echo
      echo '```'
      tail -40 "$OUT/install.log"
      echo '```'
    } > "$SUMMARY"
    exit 1
  fi
  [ -f package-lock.json ] || [ -f pnpm-lock.yaml ] && log "install=pass"
else
  log "install=skipped"
fi

script_exists() {
  node -e "
    const s = JSON.parse(require('fs').readFileSync('package.json', 'utf8')).scripts || {};
    process.exit(s['$1'] ? 0 : 1);
  " 2>/dev/null
}

# ------------------------------------------------------------- blocking -----
# A check is run only if the repo declares it. Absent checks are reported,
# never faked: an empty result is not a passing result.
for s in lint typecheck test check:claims; do
  if script_exists "$s"; then
    if npm run --silent "$s" >"$OUT/$s.log" 2>&1; then
      log "$s=pass"
    else
      log "$s=FAIL"
      fail=1
    fi
  else
    log "$s=absent"
  fi
done

# ------------------------------------------------------------- advisory -----
# Type errors in a tsconfig-only repo (no typecheck script): report, don't fail.
if script_exists typecheck >/dev/null 2>&1; then
  :
elif [ -f tsconfig.json ] && [ -x node_modules/.bin/tsc ]; then
  if node_modules/.bin/tsc --noEmit >"$OUT/tsc.log" 2>&1; then
    log "tsc_advisory=pass"
  else
    log "tsc_advisory=WARN"
  fi
else
  log "tsc_advisory=absent"
fi

if [ "${SWEEP_SKIP_AUDIT:-0}" != "1" ] && [ "$has_lock" = 1 ]; then
  if npm audit --omit=dev --json >"$OUT/audit.json" 2>&1; then
    log "audit=pass"
  else
    highs=$(OUT="$OUT" node -e "try{const o=require(process.env.OUT+'/audit.json');const m=(o.metadata||{}).vulnerabilities||{};console.log((m.high||0)+(m.critical||0))}catch(e){console.log('unknown')}")
    log "audit=high_or_critical=$highs"
  fi
  npm outdated --json >"$OUT/outdated.json" 2>/dev/null || true
  if [ -s "$OUT/outdated.json" ]; then
    count=$(OUT="$OUT" node -e "try{const o=require(process.env.OUT+'/outdated.json');console.log(Object.keys(o).length)}catch(e){console.log(0)}")
    log "outdated=$count"
  else
    log "outdated=0"
  fi
else
  log "audit=skipped"
  log "outdated=skipped"
fi

# --------------------------------------------------------------- summary ----
{
  echo "# staff-sweep results — $(date -u '+%Y-%m-%d %H:%M UTC')"
  echo
  echo "| check | result |"
  echo "|---|---|"
  while IFS='=' read -r k v; do
    echo "| \`$k\` | $v |"
  done < "$RESULTS"
  echo
  if [ "$fail" = 0 ]; then
    echo "All blocking checks that this repo declares passed."
  else
    echo "**Failures — log tails:**"
    echo
    while IFS='=' read -r k v; do
      [ "$v" = "FAIL" ] || continue
      f="$OUT/$k.log"
      [ -f "$f" ] || continue
      echo "<details><summary>$k (last 40 lines)</summary>"
      echo
      echo '```'
      tail -40 "$f"
      echo '```'
      echo "</details>"
      echo
    done < "$RESULTS"
  fi
} > "$SUMMARY"

exit "$fail"

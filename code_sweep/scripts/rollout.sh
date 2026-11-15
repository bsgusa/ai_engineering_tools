#!/usr/bin/env bash
# rollout.sh — install the code_sweep workflows into target repos as DRAFT PRs.
#
# Control-plane rollout: for each repo in config.json (or --repos), this script
#   1. shallow-clones into a temp dir (never touches your local checkouts or
#      anyone else's uncommitted work),
#   2. writes .github/workflows/{staff-sweep,ai-improve}.yml with that repo's
#      staggered cron,
#   3. commits on `feat/code-sweep-engine` as Shiv Goud (the identity Vercel
#      recognizes — see ROADMAP Blocked-deploy lesson), pushes, opens a DRAFT PR,
#   4. sets the AI_GATEWAY_API_KEY secret (from a local env file — the value is
#      never printed or logged) and the AI_IMPROVE_ENABLED variable,
#   5. creates the automated:* labels.
#
# It NEVER pushes to the default branch and NEVER merges.
#
# Usage:
#   rollout.sh                     # all enabled repos from config.json
#   rollout.sh --tier money        # only money tier
#   rollout.sh --repos a,b,c       # explicit list
#   rollout.sh --dry-run           # show what would happen, change nothing
#   rollout.sh --no-secrets        # workflows only, skip secret/variable setup
#
# Env:
#   AI_GATEWAY_KEY_FILE  file containing a line AI_GATEWAY_API_KEY=...
#   (default search: ~/bala-ai/.env.local, ~/.config/balamonis/secrets.env)

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(dirname "$HERE")"                # …/code_sweep
TPL="$ENGINE_DIR/workflows"
CONFIG="$ENGINE_DIR/config.json"
OPER="Shiv Goud"
OPEMAIL="bsgouds.usa@gmail.com"
ORG="bsgusa"
BRANCH="feat/code-sweep-engine"

TIER=""
REPOS=""
DRY=0
NO_SECRETS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tier) TIER="$2"; shift 2 ;;
    --repos) REPOS="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --no-secrets) NO_SECRETS=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

die() { echo "FATAL: $*" >&2; exit 1; }

# ------------------------------------------------------------- preflight ----
command -v gh >/dev/null || die "gh CLI not found"
gh auth status >/dev/null 2>&1 || die "gh not authenticated"
command -v python3 >/dev/null || die "python3 not found"
[ -f "$TPL/staff-sweep.yml" ] && [ -f "$TPL/ai-improve.yml" ] || die "workflow templates missing under $TPL"

# The target workflows reference the engine by tag — it must exist on origin.
ENGINE_REF=$(python3 -c "import json;print(json.load(open('$CONFIG'))['engine_ref'])")
if ! git ls-remote --exit-code "https://github.com/${ORG}/ai_engineering_tools.git" "refs/tags/$ENGINE_REF" >/dev/null 2>&1; then
  die "engine tag $ENGINE_REF not found on origin — push the branch and tag first:
  git -C ~/ai_engineering_tools tag $ENGINE_REF feat/code-sweep-engine
  git -C ~/ai_engineering_tools push origin feat/code-sweep-engine $ENGINE_REF"
fi

# Find the gateway key without ever printing it.
KEY_FILE="${AI_GATEWAY_KEY_FILE:-}"
if [ -z "$KEY_FILE" ]; then
  for f in "$HOME/bala-ai/.env.local" "$HOME/.config/balamonis/secrets.env"; do
    [ -f "$f" ] && grep -q '^AI_GATEWAY_API_KEY=' "$f" && { KEY_FILE="$f"; break; }
  done
fi
KEY=""
if [ -n "$KEY_FILE" ] && [ -f "$KEY_FILE" ]; then
  KEY=$(grep -m1 '^AI_GATEWAY_API_KEY=' "$KEY_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
fi
[ -z "$KEY" ] && echo "WARN: AI_GATEWAY_API_KEY not found — workflows will install but AI pass reports no_key until you set it."

# ----------------------------------------------------------- repo list ------
REPO_LIST=$(python3 -c "
import json, sys
cfg = json.load(open('$CONFIG'))
tier = '$TIER'
explicit = [r for r in '$REPOS'.split(',') if r]
rows = []
for r in cfg['repos']:
    if explicit and r['name'] not in explicit: continue
    if tier and r['tier'] != tier: continue
    if not r.get('enabled', True): continue
    rows.append('{name}|{sweep_cron}|{ai_cron}|{enabled}'.format(**r))
print('\n'.join(rows))
")
[ -n "$REPO_LIST" ] || die "no repos selected (tier='$TIER' repos='$REPOS')"

echo "=== code_sweep rollout — engine tag: $ENGINE_REF ==="
echo "$REPO_LIST" | while IFS='|' read -r _s _a _e; do echo "  - $_s"; done
[ "$DRY" = 1 ] && echo "(dry run — nothing will change)"
echo

ok=0; skipped=0; failed=0
while IFS='|' read -r name sweep_cron ai_cron enabled; do
  [ -n "$name" ] || continue
  repo="$ORG/$name"
  echo "--- $repo (sweep: '$sweep_cron', ai: '$ai_cron')"

  if [ "$DRY" = 1 ]; then
    echo "    [dry] would clone, write 2 workflows, push $BRANCH, open draft PR, set secret+var"
    continue
  fi

  if ! gh repo view "$repo" --json name >/dev/null 2>&1; then
    echo "    FAIL: repo not visible"; failed=$((failed+1)); continue
  fi

  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  if ! gh repo clone "$repo" "$tmp" -- --depth 1 --quiet >/dev/null 2>&1; then
    echo "    FAIL: clone failed"; failed=$((failed+1)); rm -rf "$tmp"; continue
  fi
  cd "$tmp" || { failed=$((failed+1)); rm -rf "$tmp"; continue; }

  mkdir -p .github/workflows
  sed "s/SWEEP_CRON/$sweep_cron/" "$TPL/staff-sweep.yml" > .github/workflows/staff-sweep.yml
  sed "s/AI_CRON/$ai_cron/"       "$TPL/ai-improve.yml"   > .github/workflows/ai-improve.yml

  # status --porcelain, not diff: workflows are untracked in repos that don't have them yet
  if [ -z "$(git status --porcelain -- .github/workflows/staff-sweep.yml .github/workflows/ai-improve.yml)" ]; then
    echo "    workflows already identical on default branch"
    already=1
  else
    already=0
  fi

  if [ "$already" = 0 ]; then
    git checkout -q -b "$BRANCH"
    git add .github/workflows/staff-sweep.yml .github/workflows/ai-improve.yml
    if git diff --cached --quiet; then
      echo "    nothing to commit"
    else
      git -c user.name="$OPER" -c user.email="$OPEMAIL" commit -q \
        -m "feat(sweep): nightly staff-sweep + weekly ai-improve (draft-only, control plane)" \
        -m "Engine: bsgusa/ai_engineering_tools@$ENGINE_REF. Workflows never push to the default branch and never merge; the owner merges."
      if git -c credential.helper='!gh auth git-credential' push -q -u origin "$BRANCH" 2>"$tmp/push.err"; then
        echo "    pushed branch $BRANCH"
      else
        echo "    FAIL: push — $(tail -1 "$tmp/push.err")"; failed=$((failed+1)); cd /; rm -rf "$tmp"; continue
      fi
    fi

    # draft PR (skip if one is already open for this head)
    open=$(gh pr list --repo "$repo" --head "$BRANCH" --state open --json number --jq '.length' 2>/dev/null || echo 0)
    if [ "${open:-0}" = 0 ]; then
      prbody=$(mktemp)
      cat > "$prbody" <<EOF
## Automated improvement engine — draft, awaiting your merge

Adds two scheduled workflows (control plane: \`bsgusa/ai_engineering_tools@$ENGINE_REF\`):

| workflow | cadence | what it does |
|---|---|---|
| \`staff-sweep\` | \`${sweep_cron}\` | lint / typecheck / test / claims-check, dependency audit. Failures land in **one rolling issue** (\`automated:sweep\`); safe \`npm audit fix\` changes arrive as a **draft PR**. |
| \`ai-improve\` | \`${ai_cron}\` + manual | Weekly staff-engineer LLM pass: ranked findings + upstream open-source pattern opportunities → rolling issue \`automated:ai\`. Run **manually with a brief** to have it build a validated draft PR. |

### Safety rails (by design)
- **Never pushes to the default branch, never merges.** Everything is a draft PR you approve.
- AI pass is gated by the Actions variable \`AI_IMPROVE_ENABLED\` and one secret; **one gateway call per run** with a hard output-token cap (est. a few cents per run on \`gpt-4o-mini\`).
- Model-written files are blocked from \`.github/\`, lockfiles, \`package.json\`, \`.env*\`, migrations — and every change is re-validated (install + declared checks) before this PR's equivalent opens. Failed validation is reverted and reported.
- Findings forbid fabricated claims/customers/metrics (matches your truthful-claims CI).

### After you merge
- Workflows activate from the default branch (scheduled runs start at the next cron slot; \`workflow_dispatch\` becomes available on the Actions tab).
- If GitHub is set to read-only workflow permissions, set the repo's Actions → Workflow permissions to **read and write** or the PR/issue steps will warn.
- \`ai-improve\` PRs opened with the default \`GITHUB_TOKEN\` won't trigger \`pull_request\` CI (GitHub recursion guard) — validation lives in the sweep run linked in each PR body.

*Opened by rollout.sh. Engine docs: \`code_sweep/README.md\` in \`bsgusa/ai_engineering_tools\`.*
EOF
      if gh pr create --repo "$repo" --draft \
          --head "$BRANCH" --base "$(gh repo view "$repo" --json defaultBranchRef --jq .defaultBranchRef.name)" \
          --title "feat(sweep): automated code-improvement engine (draft)" \
          --body-file "$prbody" 2>"$tmp/pr.err"; then
        echo "    draft PR opened"
        ok=$((ok+1))
      else
        echo "    FAIL: pr create — $(tail -1 "$tmp/pr.err")"; failed=$((failed+1))
      fi
      rm -f "$prbody"
    else
      echo "    PR already open — not duplicating"
      skipped=$((skipped+1))
    fi
  else
    skipped=$((skipped+1))
  fi

  # secrets + variables (independent of PR state so the workflow works post-merge)
  if [ "$NO_SECRETS" = 0 ]; then
    if [ -n "$KEY" ]; then
      if printf '%s' "$KEY" | gh secret set AI_GATEWAY_API_KEY --repo "$repo" 2>/dev/null; then
        echo "    secret AI_GATEWAY_API_KEY set"
      else
        echo "    WARN: could not set secret (permissions)"
      fi
    fi
    if gh variable set AI_IMPROVE_ENABLED --repo "$repo" --body true 2>/dev/null; then
      echo "    variable AI_IMPROVE_ENABLED=true"
    else
      echo "    WARN: could not set variable (permissions)"
    fi
    # verified live 2026-11-15: 4o-mini mangles build-mode file rewrites (it gutted
    # a 68-line file to 4 lines on an "add a comment" brief — caught by the
    # destructive guard). gpt-5-mini followed the same brief correctly and found
    # materially better scan findings. Cost: est $0.015/run → ~$0.40/month/repo-set.
    if gh variable set AI_IMPROVE_MODEL --repo "$repo" --body "openai/gpt-5-mini" 2>/dev/null; then
      echo "    variable AI_IMPROVE_MODEL=openai/gpt-5-mini"
    else
      echo "    WARN: could not set model variable (permissions)"
    fi
  fi

  gh label create automated:sweep --repo "$repo" --color D93F0B \
    --description "Automated sweep findings — human triages" --force >/dev/null 2>&1 || true
  gh label create automated:ai --repo "$repo" --color 0E8A16 \
    --description "AI improvement findings — human triages" --force >/dev/null 2>&1 || true

  cd /
  rm -rf "$tmp"
  trap - EXIT
done <<< "$REPO_LIST"

echo
echo "=== rollout summary: opened=$ok skipped=$skipped failed=$failed (dry=$DRY) ==="
[ "$failed" = 0 ] || exit 1

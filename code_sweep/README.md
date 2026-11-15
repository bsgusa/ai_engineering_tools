# code_sweep — the 24/7 code-improvement engine

Runs continuously on GitHub Actions so it does not need your Mac awake. It works
two jobs, in two workflows installed into each target repo by `rollout.sh`:

| workflow | cadence | what it does | writes |
|---|---|---|---|
| `staff-sweep` | nightly (staggered cron) | lint, typecheck, tests, truthful-claims check, `npm audit`, outdated deps — everything the repo actually declares, nothing faked | run summary; **one rolling issue** on failure (`automated:sweep`); validated `npm audit fix` as a **draft PR** |
| `ai-improve` | weekly + manual | staff-engineer LLM pass over a byte-budgeted slice of the repo: ranked findings + **upstream open-source pattern opportunities** (cite real sources, license-checked) | summary + rolling issue `automated:ai`; with a human **brief**: validated **draft PR** |

**Both workflows only ever draft. Nothing here merges, pushes to a default
branch, deploys, or sends anything.** That is the standing owner rule
("AI drafts, you decide") implemented in `permissions` + branch discipline, not
promised in prose.

## Modes

- **Autonomous (scheduled):** findings only. The model is instructed `changes: []`.
  You triage the rolling issue.
- **Build (manual dispatch with a brief):** you hand it a work order ("fix the
  race in X", "add Y endpoint"). The model may rewrite up to 3 allowlisted files;
  the engine then reruns install + declared checks. Green → draft PR. Red →
  changes reverted, failure tail published. Either way the repo is left clean.

## Architecture (control plane)

```
bsgusa/ai_engineering_tools  (this repo, public)
└── code_sweep/
    ├── config.json          # repo tiers, staggered crons, token/byte budgets
    ├── workflows/*.yml      # thin templates (cron placeholder substituted at rollout)
    └── scripts/
        ├── sweep.sh         # deterministic check runner (no LLM)
        ├── ai-improve.mjs   # LLM pass: scan | build, path guard, validation
        └── rollout.sh       # installs into repos as draft PRs + sets secret/var

target repo (e.g. bsgusa/bala-ai)
└── .github/workflows/
    ├── staff-sweep.yml      # checks out the engine at tag code-sweep-v1 into $RUNNER_TEMP
    └── ai-improve.yml
```

Engine updates: push a new tag (`code-sweep-v2`), run
`rollout.sh` — it opens one-line ref-bump PRs per repo. Target repos never
contain sweep logic, so there is one place to fix, audit, or kill.

## Budget & cost (honest numbers)

- **1 gateway call per run**, `max_tokens` 12 000, context byte-budgeted
  (≤ 60 000 chars, ≤ 14 files, ≤ 12 000 chars/file) from `config.json`.
- Rollout sets `AI_IMPROVE_MODEL=openai/gpt-5-mini` per repo. Measured
  **2026-11-15: $0.0146 per scan run** (16.7k in / 5.2k out incl. reasoning) →
  all six repos weekly ≈ **$0.40/month total**. The default in `config.json`
  stays `gpt-4o-mini` for local runs without the variable.
- Every run prints tokens + est. cost to the step summary.
- Set repo variable `AI_IMPROVE_MODEL` to change the model; set
  `AI_IMPROVE_ENABLED=false` to silence one repo without a PR.

**Why gpt-5-mini over the cheap model (tested, not assumed):** on the same
"add a doc comment" brief, `gpt-4o-mini` returned a gutted file (68 → 4 lines,
implementation deleted) — and lint+tests still passed, because a file of
comments is valid JavaScript. `gpt-5-mini` produced a correct 72-line change.
The destructive-shrink guard below exists because of that live failure.

## Safety rails (do not loosen without an owner decision)

1. Draft-only: `permissions` are write-scoped to enable PR/issue creation, but
   every path lands on a branch, never `main`.
2. Path guard in `ai-improve.mjs`: blocked from `.github/**`, lockfiles,
   `package.json`, `.env*`, `supabase/migrations/**`, `node_modules/**`;
   no absolute paths, no `..`.
3. The model cannot add dependencies (findings say "needs dep X" instead).
4. Validation before any PR: the same `sweep.sh` used nightly re-runs on the
   model's changes; failure reverts the tree. Plus a **destructive-shrink
   guard**: an existing file shrinking to <35% of its lines is reverted unless
   the human brief explicitly asked for deletion/rewrite — because green
   lint+tests do not prove the model followed the brief (live-tested failure,
   see Budget & cost).
5. Findings prompt forbids fabricated claims/customers/metrics and forbids
   pasting GPL/AGPL/SSPL code — upstream help is pattern-reimplementation with
   a cited source and license field.
6. `api_error` / `engine_error` **fail the job** (GitHub emails you) — silent
   failure is the exact failure mode this engine exists to remove (see the
   pyyaml landmine in this repo's ROADMAP).

## Known limitations (stated, not hidden)

- PRs opened with the default `GITHUB_TOKEN` do **not** trigger
  `pull_request` workflows (GitHub recursion guard). Validation is the sweep run
  linked in each PR body. If you shrink the PAT to `repo`+`workflow` (B-031),
  you can point a `SWEEP_TOKEN` secret at it to get CI-on-PR too.
- GitHub disables scheduled workflows after **60 days without repo activity** —
  the weekly cadence doubles as the keep-alive, but a fully dormant repo will
  go quiet (as designed).
- This is a code/CI/dependency sweep, **not** a security scanner. Add gitleaks
  or similar as a separate blocking check when you want that.
- Findings are model judgments. The rolling issues exist so a human triages;
  nothing auto-applies outside the explicit brief path.

## Usage

```bash
# 1. publish the engine (once), tag is what target workflows pin
cd ~/ai_engineering_tools
git tag code-sweep-v1 feat/code-sweep-engine
git push origin feat/code-sweep-engine code-sweep-v1

# 2. install into repos as draft PRs (money tier first)
bash code_sweep/scripts/rollout.sh --tier money --dry-run   # review
bash code_sweep/scripts/rollout.sh --tier money             # real
bash code_sweep/scripts/rollout.sh --tier core

# 3. owner: review + merge the draft PRs; runs start at the next cron slot

# 4. build mode: Actions tab → ai-improve → Run workflow → fill "brief"
```

Local testing (no CI):

```bash
cd /path/to/repo
SWEEP_OUT=/tmp/sweep bash ~/ai_engineering_tools/code_sweep/scripts/sweep.sh
node ~/ai_engineering_tools/code_sweep/scripts/ai-improve.mjs --dry-run   # writes prompt only
node ~/ai_engineering_tools/code_sweep/scripts/ai-improve.mjs             # real call, needs AI_GATEWAY_API_KEY
```

## Repo tiers

`config.json` marks money repos (balamonis.com, lexoraai.org, lexoraai.space,
bala-ai) and core repos. Static `bala-*` docs repos and empty repos are
**excluded by design** — a sweep over docs with no build re-reports an empty
result forever (recorded in `excluded_by_design`).

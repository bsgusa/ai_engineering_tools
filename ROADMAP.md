# Roadmap — AI Engineering Toolkit

AI-native automation layer that understands your work and turns "pending" into
"action." Scans trading ledgers, job-automation apps, inbox files, and BloFin
account state; scores each item (`impact × urgency / (effort + 1)`); writes
first-draft emails, Slack messages, reports, commits, and trade plans — you
review and hit send. Dry-run by default; nothing is sent, committed, or traded
unless you explicitly execute.

## [NOW] — Harden the tracker + ship the weekly review

- [x] `pending_tracker.py` — JSONL + SQLite tracker, AI scoring, CRUD, export
- [x] `ai_drafts.py` — first-draft generator (LLM or template fallback)
- [x] `orchestrator.py` — master loop: scan → score → draft → (optionally) execute
- [x] `weekly_review.py` — status breakdown, aging, blockers, suggested next actions
- [x] `n8n/workflows.json` — 4 workflows (trading_ledger, blofin_account, inbox_files, weekly-review), all inactive by default
- [x] `supabase/schema.sql` — `pending_items` table, RLS, service-role writes only
- [x] Dedupe command (`--dedupe`) to collapse duplicate runs
- [ ] Init git repo + push to GitHub (blocked on shell DNS / GitHub credentials)
- [ ] Wire Supabase sync (optional mirror; local JSONL stays source of truth)

## [NEXT] — AI self-improvement + execution hooks

- [ ] **B-010** Self-improving prompt templates — toolkit reviews its own drafts and refines them
- [ ] **B-011** Cross-project pending-items graph — one graph across trading + job automation + Balamonis repos
- [ ] **B-012** Execution hooks — send/commit/trade behind existing RiskGate; `--approve-all` guardrails
- [ ] Expand n8n to also auto-create items from Slack/email via webhook
- [ ] LLM backend wired (Ollama / OpenAI-compatible) behind `config.yaml` `llm.enabled`

## [LATER] — Force multipliers

- [ ] Daily AI standup summary emailed/Slacked each morning
- [ ] Auto-triage inbox: classify incoming email into pending items with suggested action
- [ ] AI writes commit messages and PR descriptions from pending-item drafts
- [ ] Trade drafting: engine proposes carry entries, toolkit writes the trade draft for manual approval

## AI integration (throughout)

Every source scan produces an AI-drafted first action. The LLM is optional —
deterministic templates always work. Scoring, drafting, and execution are
separate concerns so you can swap in any model or provider without touching the
tracker.

## Rules for this repo

- Work on a branch, never `main`. Small commits with clear messages.
- Dry-run by default; `--execute` required to act.
- Never commit secrets. `config.yaml` is gitignored; copy from `config.example.yaml`.
- Before ending a session: run the orchestrator dry-run, update this roadmap,
  and append a dated entry to `reports/`.

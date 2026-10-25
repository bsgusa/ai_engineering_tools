# AI Engineering Toolkit

A modular, inspectable automation layer that understands your work and turns
"pending" into "action." Built to integrate with your existing stacks:

- `~/trading` — BloFin delta-neutral carry bot (Python, stdlib, Supabase ledger, RiskGate)
- `~/AI-Job-Automation` — Lexora job-automation web stack (Supabase, Next.js-ish)
- `n8n` — workflow automation
- Anything else you point it at

## Philosophy

- **AI drafts, you decide.** The toolkit writes first drafts (email, Slack, report, code PR). You hit send/commit/trade.
- **Never blocks.** Supabase sync, LLM calls, and n8n are all optional. The local JSONL store always works.
- **Dry-run by default.** `--execute` is required to actually send or place anything.
- **You own it.** Every file is plain Python/YAML/SQL. Read it, modify it, delete what you don't need.

## Structure

```
ai_engineering_tools/
  README.md              # this file
  config.example.yaml    # copy to config.yaml and fill in
  orchestrator.py        # master loop: scan → triage → draft → queue/execute
  pending_tracker.py     # CRUD + scoring for pending items
  ai_drafts.py           # first-draft generator (LLM or template fallback)
  n8n/                   # n8n workflow JSON exports
  supabase/              # SQL schema + RLS policies
  reports/               # generated weekly/daily reports (gitignored)
  state/                 # local JSONL + SQLite state (gitignored)
```

## Quick start

```bash
# 1. Configure
cp ai_engineering_tools/config.example.yaml ai_engineering_tools/config.yaml
# edit config.yaml with your paths, LLM endpoint, Supabase creds

# 2. Dry-run scan of everything (safe, writes nothing)
python3 ai_engineering_tools/orchestrator.py --dry-run

# 3. Review generated drafts in ai_engineering_tools/reports/

# 4. Execute only the actions you approve
python3 ai_engineering_tools/orchestrator.py --execute --approve-all   # or approve individually
```

## How it understands your work

The orchestrator scans configurable "sources":

| Source | What it looks for | Example pending items |
|--------|-------------------|-----------------------|
| Trading ledger (`ledger.jsonl`) | Rejected signals, open positions, drift, risk events | "REJECTED: max book exceeded on BTC-USDT", "drift: exchange has position, ledger has none" |
| Trading `reports/` | Stale or missing daily reports | "daily report not written for 2026-10-21" |
| BloFin account (via `.env`) | Missing credentials, balance drift | "BLOFIN_API_PASSPHRASE not set — live orders locked" |
| Job automation (Lexora/Supabase) | Applications in `applied`/`interviewing` with no follow-up | "Job at Acme — interviewed 3 days ago, no thank-you sent" |
| Inbox / files you point it at | Emails, Slack exports, notes marked `TODO`/`PENDING` | Any item you tag |

Each item gets: an ID, source, title, body, status, AI score (impact × urgency × effort), a suggested action, and a draft.

## AI scoring

```
score = impact × urgency / (effort + 1)
```

- **Impact:** 1–5 (how much does this matter to your goals?)
- **Urgency:** 1–5 (how soon does it need attention?)
- **Effort:** 1–5 (how hard is it to do?)

Higher score = higher priority. You can override the score or the suggested action at any time.

## Modifying it

- Add a new source: subclass `Source` in `orchestrator.py`, register it in `SOURCES`.
- Change scoring: edit `score_item()` in `pending_tracker.py`.
- Change drafts: edit `ai_drafts.py` — swap in your preferred LLM endpoint or prompt templates.
- Add an action hook: add an `execute_*` function in `orchestrator.py` and register it in `ACTIONS`.

## Safety

- No live trading happens unless you set `TRADING_MODE=live` + `TRADING_CONFIRM=I_UNDERSTAND_REAL_MONEY` (matching your existing `executor.py` lock).
- No email/Slack is sent unless you pass `--execute` and approve the specific draft.
- Supabase writes use a service-role key only; a sync failure never blocks the run.

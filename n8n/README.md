# n8n workflow templates

Import these into your n8n instance (Settings → Import) to auto-create pending items
from email, Slack, and trading alerts.

## workflows/trading-alerts-to-pending.json

**Trigger:** BloFin webhook / scheduled cron (every 15 min) → run `python3 orchestrator.py --source trading_ledger --execute` via an Execute Command node.

**Nodes:**
1. Cron (every 15 minutes)
2. Execute Command — `python3 /Users/shivgoud/ai_engineering_tools/orchestrator.py --source trading_ledger`
3. Set — map output to n8n variables (optional notifications)

## workflows/inbox-to-pending.json

**Trigger:** IMAP/Gmail node (new email with label `TODO` or `PENDING`) → create pending item.

**Nodes:**
1. Gmail/IMAP trigger (new email)
2. Set — extract subject → title, body → body, from → action_target
3. HTTP Request — POST to your orchestrator webhook (or write directly to the pending.jsonl ledger)
4. (Optional) Slack — notify you of the new pending item

## workflows/slack-reminder.json

**Trigger:** Scheduled (daily 09:00) → read pending items → Slack reminder.

**Nodes:**
1. Cron (daily)
2. Execute Command — `python3 pending_tracker.py --list --status open --export json`
3. Slack — post message: "Here are your open pending items today..."

## How to connect

- The orchestrator writes to `./state/pending.jsonl`. n8n can read that file directly via Read Binary File / IF nodes.
- Or expose a small HTTP endpoint wrapping `pending_tracker.py --add` if you want n8n to push items in.
- Keep n8n writes separate from the trading ledger — never let n8n touch `~/trading/ledger.jsonl`.

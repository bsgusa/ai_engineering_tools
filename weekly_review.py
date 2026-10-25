#!/usr/bin/env python3
"""Weekly review report generator for the AI Engineering Toolkit.

Reads pending items from the local tracker (JSONL + SQLite) and produces a
markdown review with:
  - status breakdown (open / in_progress / blocked / done / dismissed)
  - aging: how long items have been open
  - blockers: stale or high-score items still open
  - auto-actioned items (executed vs skipped)
  - suggested next actions (top-scoring open items)

Usage:
  python3 weekly_review.py                 # print to stdout
  python3 weekly_review.py --write         # save to reports/weekly-YYYY-MM-DD.md
  python3 weekly_review.py --days 7        # only items from the last N days
  python3 weekly_review.py --source trading_ledger  # filter by source
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from pending_tracker import PendingTracker, STATUSES  # noqa: E402

REPORTS_DIR = Path(os.environ.get("AI_TOOLS_REPORTS_DIR", "./reports"))
STATE_DIR = Path(os.environ.get("AI_TOOLS_STATE_DIR", "./state"))
LEDGER_FILE = STATE_DIR / "pending.jsonl"


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(created_at):
    dt = parse_iso(created_at)
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400


def load_items(days=None, source=None):
    tracker = PendingTracker(ledger_file=LEDGER_FILE)
    rows = tracker.list(limit=10000, order_by="created_at")
    if source:
        rows = [r for r in rows if r.get("source") == source]
    if days is not None:
        cutoff = time.time() - days * 86400
        kept = []
        for r in rows:
            dt = parse_iso(r.get("created_at", ""))
            if dt and dt.timestamp() >= cutoff:
                kept.append(r)
        rows = kept
    return rows


def build_report(rows, days=None, source=None):
    total = len(rows)
    by_status = Counter(r.get("status", "open") for r in rows)
    by_source = Counter(r.get("source", "manual") for r in rows)

    # Aging for open items
    open_items = [r for r in rows if r.get("status") == "open"]
    aging = []
    for r in open_items:
        a = age_days(r.get("created_at", ""))
        if a is not None:
            aging.append({"id": r["id"], "title": r["title"], "source": r.get("source", ""),
                          "score": r.get("score", 0), "age_days": round(a, 1),
                          "impact": r.get("impact"), "urgency": r.get("urgency"),
                          "effort": r.get("effort")})
    aging.sort(key=lambda x: x["age_days"], reverse=True)

    # Blockers: high-score open items, or open items aged > 7 days
    blockers = [a for a in aging if a["score"] >= 6.0 or a["age_days"] > 7]
    blockers.sort(key=lambda x: x["score"], reverse=True)

    # Suggested next actions: top 5 open by score
    next_actions = sorted(open_items, key=lambda r: r.get("score", 0), reverse=True)[:5]

    # Score stats
    scores = [r.get("score", 0) for r in rows]
    avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
    max_score = round(max(scores), 2) if scores else 0.0

    lines = []
    period = f"last {days} days" if days else "all time"
    src_filter = f" (source: {source})" if source else ""
    lines.append(f"# Weekly Review — AI Engineering Toolkit")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Period: {period}{src_filter}")
    lines.append("")

    lines.append("## Summary")
    lines.append(f"- Total pending items tracked: **{total}**")
    lines.append(f"- Average score: {avg_score}  |  Highest score: {max_score}")
    lines.append("")
    lines.append("### By status")
    for status in STATUSES:
        n = by_status.get(status, 0)
        if n:
            lines.append(f"- {status}: {n}")
    lines.append("")
    lines.append("### By source")
    for src, n in by_source.most_common():
        lines.append(f"- {src}: {n}")
    lines.append("")

    lines.append("## Blockers (attention needed)")
    if not blockers:
        lines.append("- No blockers. All open items are low-score and recent.")
    else:
        for b in blockers[:10]:
            lines.append(
                f"- [{b['id']}] {b['title']} — score {b['score']} "
                f"(age {b['age_days']}d, impact {b['impact']}, urgency {b['urgency']}, "
                f"effort {b['effort']})  ·  {b['source']}"
            )
    lines.append("")

    lines.append("## Open items by age (oldest first)")
    if not aging:
        lines.append("- No open items.")
    else:
        for a in aging[:15]:
            lines.append(
                f"- [{a['id']}] {a['title']} — {a['age_days']}d old, score {a['score']}  ·  {a['source']}"
            )
    lines.append("")

    lines.append("## Suggested next actions (top 5 by score)")
    if not next_actions:
        lines.append("- Nothing open. Catch up!")
    else:
        for r in next_actions:
            lines.append(
                f"- [{r['id']}] {r['title']} — score {r.get('score', 0)} "
                f"({r.get('impact')}×{r.get('urgency')}/({r.get('effort')}+1))  ·  "
                f"suggested: {r.get('suggested_action', 'review')} → {r.get('action_target', '-') or '-'}"
            )
    lines.append("")

    lines.append("## Recently completed / dismissed")
    done = [r for r in rows if r.get("status") in ("done", "dismissed")]
    if not done:
        lines.append("- None yet.")
    else:
        for r in done:
            lines.append(f"- [{r['id']}] {r['title']} — {r.get('status')}")
    lines.append("")

    lines.append("---")
    lines.append("_Generated by the AI Engineering Toolkit weekly review. "
                 "Review, re-score, or dismiss items with `python3 pending_tracker.py --update --id <id>`._")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Weekly review report for pending items")
    ap.add_argument("--write", action="store_true", help="save to reports/")
    ap.add_argument("--days", type=int, default=None, help="only items from last N days")
    ap.add_argument("--source", default=None, help="filter by source name")
    ap.add_argument("--limit", type=int, default=10000, help="max items to read")
    args = ap.parse_args()

    rows = load_items(days=args.days, source=args.source)
    report = build_report(rows, days=args.days, source=args.source)
    print(report, end="")

    if args.write:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        tag = f"{args.days}d" if args.days else "all"
        src_tag = f"-{args.source}" if args.source else ""
        path = REPORTS_DIR / f"weekly-review-{tag}{src_tag}-{time.strftime('%Y-%m-%d')}.md"
        path.write_text(report)
        print(f"\nWritten: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

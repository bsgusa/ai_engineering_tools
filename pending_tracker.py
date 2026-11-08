#!/usr/bin/env python3
"""Pending-items tracker with AI scoring.

Local source of truth: a JSONL file (same pattern as your trading ledger).
Optional SQLite for faster queries. Optional Supabase sync (never blocks).

Every pending item:
  {
    "id": "pending-<uuid8>",
    "source": "trading_ledger|job_automation|inbox|manual",
    "title": "short human-readable title",
    "body": "details",
    "status": "open|in_progress|blocked|done|dismissed",
    "score": 0.0,                 # impact * urgency / (effort + 1)
    "impact": 3, "urgency": 3, "effort": 3,   # 1-5
    "suggested_action": "what to do next",
    "draft": "first draft of the action (email/slack/report/commit msg)",
    "action_type": "email|slack|report|commit|trade|review|none",
    "action_target": "who/what it's for (email address, symbol, repo, etc.)",
    "due": "2026-10-23",          # ISO date, optional
    "created_at": "...",
    "updated_at": "...",
    "metadata": {}                # source-specific extras (symbol, order_id, job_id, etc.)
  }

Usage:
  python3 pending_tracker.py --add --source manual --title "..." --body "..."
  python3 pending_tracker.py --list --status open
  python3 pending_tracker.py --score --id pending-xxxx
  python3 pending_tracker.py --export --format md
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

try:
    import yaml  # optional, for config loading
except ImportError:
    yaml = None

STATE_DIR = Path(os.environ.get("AI_TOOLS_STATE_DIR", "./state"))
REPORTS_DIR = Path(os.environ.get("AI_TOOLS_REPORTS_DIR", "./reports"))
LEDGER_FILE = STATE_DIR / "pending.jsonl"
DB_FILE = STATE_DIR / "pending.db"

STATUSES = ("open", "in_progress", "blocked", "done", "dismissed")
ACTION_TYPES = ("email", "slack", "report", "commit", "trade", "review", "none")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_ms():
    return int(time.time() * 1000)


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


class PendingTracker:
    def __init__(self, ledger_file=LEDGER_FILE, db_file=DB_FILE):
        self.ledger_file = Path(ledger_file)
        self.db_file = Path(db_file)
        ensure_dirs()
        self._init_sqlite()

    # ---------------------------------------------------------------- storage
    def _init_sqlite(self):
        try:
            con = sqlite3.connect(self.db_file)
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_items (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    body TEXT,
                    status TEXT,
                    score REAL,
                    impact INTEGER,
                    urgency INTEGER,
                    effort INTEGER,
                    suggested_action TEXT,
                    draft TEXT,
                    action_type TEXT,
                    action_target TEXT,
                    due TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    metadata TEXT
                )
                """
            )
            con.commit()
            con.close()
        except sqlite3.Error as exc:
            print(f"  sqlite init failed (falling back to JSONL only): {exc}", file=sys.stderr)

    def _append_ledger(self, rec):
        try:
            with open(self.ledger_file, "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError as exc:
            print(f"  ledger write failed: {exc}", file=sys.stderr)

    def _write_sqlite(self, rec):
        try:
            con = sqlite3.connect(self.db_file)
            con.execute(
                """
                INSERT OR REPLACE INTO pending_items
                (id, source, title, body, status, score, impact, urgency, effort,
                 suggested_action, draft, action_type, action_target, due,
                 created_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["id"], rec["source"], rec["title"], rec["body"], rec["status"],
                    rec["score"], rec["impact"], rec["urgency"], rec["effort"],
                    rec["suggested_action"], rec["draft"], rec["action_type"],
                    rec["action_target"], rec["due"], rec["created_at"],
                    rec["updated_at"], json.dumps(rec.get("metadata", {})),
                ),
            )
            con.commit()
            con.close()
        except sqlite3.Error as exc:
            print(f"  sqlite write failed: {exc}", file=sys.stderr)

    # ---------------------------------------------------------------- CRUD
    def find_open(self, source, title):
        """Return an existing non-terminal record for this source+title, else None.

        Cross-run dedupe (audit 2026-11-07): `add()` used to append a fresh
        UUID on every call, so re-running the orchestrator duplicated every
        item (5 unique titles -> 13 ledger rows after two runs). Scheduling
        that daily would have bloated the ledger one row per item per day.

        Only `open`, `in_progress` and `blocked` records match. A `done` or
        `dismissed` item is deliberately excluded: if the same signal comes
        back after being closed, that is new information and deserves its own
        record rather than silently reopening a finished one.
        """
        active = ("open", "in_progress", "blocked")
        for rec in self._read_ledger():
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            if rec.get("status") not in active:
                continue
            if rec.get("source") == source and rec.get("title") == title:
                return rec
        return None

    def add(self, source, title, body, impact=3, urgency=3, effort=3,
            suggested_action="review", draft="", action_type="review",
            action_target="", due=None, metadata=None):
        existing = self.find_open(source, title)
        if existing is not None:
            # Refresh in place: keep the original id and created_at, bump
            # updated_at so the item's aging stays truthful.
            existing["body"] = body or existing.get("body", "")
            existing["updated_at"] = now_iso()
            if metadata:
                merged = dict(existing.get("metadata") or {})
                merged.update(metadata)
                existing["metadata"] = merged
            existing["score"] = self.score(
                existing.get("impact", 3),
                existing.get("urgency", 3),
                existing.get("effort", 3),
            )
            self._rewrite_ledger(
                [existing if r.get("id") == existing["id"] else r
                 for r in self._read_ledger()]
            )
            self._write_sqlite(existing)
            existing["_refreshed"] = True
            return existing

        rec = {
            "id": f"pending-{uuid.uuid4().hex[:8]}",
            "source": source,
            "title": title,
            "body": body,
            "status": "open",
            "impact": max(1, min(5, int(impact))),
            "urgency": max(1, min(5, int(urgency))),
            "effort": max(1, min(5, int(effort))),
            "suggested_action": suggested_action,
            "draft": draft or self._default_draft(action_type, title, body),
            "action_type": action_type if action_type in ACTION_TYPES else "review",
            "action_target": action_target,
            "due": due or "",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "metadata": metadata or {},
        }
        rec["score"] = self.score(rec["impact"], rec["urgency"], rec["effort"])
        self._append_ledger(rec)
        self._write_sqlite(rec)
        # Set after both writes: `_refreshed` is a per-run signal for the
        # orchestrator, not part of the stored record.
        rec["_refreshed"] = False
        return rec

    def get(self, item_id):
        for rec in self._read_ledger():
            if rec.get("id") == item_id:
                return rec
        return None

    def list(self, status=None, source=None, limit=100, order_by="score"):
        rows = [r for r in self._read_ledger()]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if source:
            rows = [r for r in rows if r.get("source") == source]
        if order_by == "score":
            rows.sort(key=lambda r: r.get("score", 0), reverse=True)
        elif order_by == "created_at":
            rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        elif order_by == "due":
            rows.sort(key=lambda r: r.get("due") or "")
        return rows[:limit]

    def update(self, item_id, **fields):
        rec = self.get(item_id)
        if not rec:
            return None
        allowed = {
            "status", "title", "body", "impact", "urgency", "effort",
            "suggested_action", "draft", "action_type", "action_target",
            "due", "metadata",
        }
        for k, v in fields.items():
            if k in allowed:
                rec[k] = v
        if {"impact", "urgency", "effort"} & set(fields):
            rec["score"] = self.score(rec["impact"], rec["urgency"], rec["effort"])
        rec["updated_at"] = now_iso()
        # rewrite ledger with updated record
        self._rewrite_ledger([rec if r.get("id") == item_id else r for r in self._read_ledger()])
        self._write_sqlite(rec)
        return rec

    def delete(self, item_id):
        rows = [r for r in self._read_ledger() if r.get("id") != item_id]
        self._rewrite_ledger(rows)
        try:
            con = sqlite3.connect(self.db_file)
            con.execute("DELETE FROM pending_items WHERE id = ?", (item_id,))
            con.commit()
            con.close()
        except sqlite3.Error:
            pass
        return True

    # ---------------------------------------------------------------- scoring
    @staticmethod
    def score(impact, urgency, effort):
        """impact × urgency / (effort + 1). Higher = more important."""
        return round((float(impact) * float(urgency)) / (float(effort) + 1), 2)

    @staticmethod
    def _default_draft(action_type, title, body):
        templates = {
            "email": f"Subject: Action needed — {title}\n\n{body}\n\n— Sent via AI Engineering Toolkit (draft, review before sending)",
            "slack": f"*Action needed:* {title}\n{body}\n_(draft — review before sending)_",
            "report": f"# {title}\n\n{body}\n\n_Generated by AI Engineering Toolkit — review before publishing._",
            "commit": f"chore: {title}\n\n{body}\n\n(pending review)",
            "trade": f"TRADE DRAFT — {title}\n{body}\n(DO NOT EXECUTE without risk gate approval)",
            "review": f"# Review: {title}\n\n{body}\n\nPlease review and take action.",
            "none": body,
        }
        return templates.get(action_type, body)

    # ---------------------------------------------------------------- internals
    def _read_ledger(self):
        rows = []
        try:
            with open(self.ledger_file) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            pass
        return rows

    def _rewrite_ledger(self, rows):
        try:
            with open(self.ledger_file, "w") as fh:
                for r in rows:
                    fh.write(json.dumps(r, default=str) + "\n")
        except OSError as exc:
            print(f"  ledger rewrite failed: {exc}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Pending-items tracker")
    ap.add_argument("--list", action="store_true", help="list pending items")
    ap.add_argument("--status", help="filter by status")
    ap.add_argument("--source", help="filter by source")
    ap.add_argument("--add", action="store_true", help="add a pending item")
    ap.add_argument("--title", help="title for new item")
    ap.add_argument("--body", help="body for new item")
    ap.add_argument("--id", help="item id to operate on")
    ap.add_argument("--score", action="store_true", help="show score for --id")
    ap.add_argument("--impact", type=int, default=None,
                    help="impact 1-5 (add defaults to 3; update keeps existing value)")
    ap.add_argument("--urgency", type=int, default=None,
                    help="urgency 1-5 (add defaults to 3; update keeps existing value)")
    ap.add_argument("--effort", type=int, default=None,
                    help="effort 1-5 (add defaults to 3; update keeps existing value)")
    ap.add_argument("--action", default=None,
                    help="suggested action type (add defaults to 'review'; update keeps existing value)")
    ap.add_argument("--target", default="", help="action target")
    ap.add_argument("--due", default="", help="due date ISO")
    ap.add_argument("--delete", action="store_true", help="delete --id from the tracker")
    ap.add_argument("--update", action="store_true", help="update an existing item")
    ap.add_argument("--set-status", help="set status on --id")
    ap.add_argument("--export", choices=["md", "json"], help="export list")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--dedupe", action="store_true", help="remove duplicate items (same source+title, keep newest)")
    args = ap.parse_args()

    tr = PendingTracker()

    if args.add:
        if not args.title:
            print("--title required with --add", file=sys.stderr)
            return 2
        rec = tr.add(
            source=args.source or "manual",
            title=args.title,
            body=args.body or "",
            impact=args.impact if args.impact is not None else 3,
            urgency=args.urgency if args.urgency is not None else 3,
            effort=args.effort if args.effort is not None else 3,
            action_type=args.action or "review",
            action_target=args.target or "",
            due=args.due or "",
        )
        print(json.dumps(rec, indent=2, default=str))
        return 0

    if args.id:
        rec = tr.get(args.id)
        if not rec:
            print(f"item not found: {args.id}", file=sys.stderr)
            return 1
        if args.delete:
            tr.delete(args.id)
            print(f"deleted {args.id}")
            return 0
        if args.update:
            fields = {}
            if args.set_status:
                fields["status"] = args.set_status
            if args.impact is not None:
                fields["impact"] = args.impact
            if args.urgency is not None:
                fields["urgency"] = args.urgency
            if args.effort is not None:
                fields["effort"] = args.effort
            if args.action:
                fields["action_type"] = args.action
            if args.target:
                fields["action_target"] = args.target
            if args.due:
                fields["due"] = args.due
            rec = tr.update(args.id, **fields)
            print(json.dumps(rec, indent=2, default=str))
        elif args.score:
            print(json.dumps({"id": rec.get("id", args.id),
                              "score": rec.get("score", 0),
                              "impact": rec.get("impact", 0),
                              "urgency": rec.get("urgency", 0),
                              "effort": rec.get("effort", 0)}, indent=2))
        else:
            print(json.dumps(rec, indent=2, default=str))
        return 0

    if args.dedupe:
        rows = tr._read_ledger()
        seen = {}
        keep = []
        malformed = 0
        for r in rows:
            if not isinstance(r, dict) or not r.get("id") or not r.get("title"):
                # Partial/malformed record: keep it in place rather than crash
                # the rewrite or silently drop data (audit 2026-11-06, B-8).
                malformed += 1
                keep.append(r)
                continue
            key = (r.get("source", ""), r.get("title", ""))
            if key in seen:
                # keep the newer one (by updated_at)
                existing_idx = seen[key]
                if r.get("updated_at", "") > keep[existing_idx].get("updated_at", ""):
                    keep[existing_idx] = r
            else:
                seen[key] = len(keep)
                keep.append(r)
        removed = len(rows) - len(keep)
        tr._rewrite_ledger(keep)
        try:
            con = sqlite3.connect(tr.db_file)
            con.execute("DELETE FROM pending_items")
            for r in keep:
                if not isinstance(r, dict) or not r.get("id"):
                    continue
                con.execute(
                    """INSERT OR REPLACE INTO pending_items
                    (id, source, title, body, status, score, impact, urgency, effort,
                     suggested_action, draft, action_type, action_target, due,
                     created_at, updated_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r.get("id", ""), r.get("source", ""), r.get("title", ""),
                        r.get("body", ""), r.get("status", ""),
                        r.get("score", 0), r.get("impact", 0), r.get("urgency", 0),
                        r.get("effort", 0), r.get("suggested_action", ""),
                        r.get("draft", ""), r.get("action_type", ""),
                        r.get("action_target", ""), r.get("due", ""),
                        r.get("created_at", ""), r.get("updated_at", ""),
                        json.dumps(r.get("metadata", {})),
                    ),
                )
            con.commit()
            con.close()
        except sqlite3.Error as exc:
            print(f"  sqlite rebuild failed (ledger kept as source of truth): {exc}",
                  file=sys.stderr)
        note = f", {malformed} malformed record(s) preserved unchanged" if malformed else ""
        print(f"deduped: {removed} duplicate(s) removed, {len(keep)} item(s) remain{note}")
        return 0

    if args.list or args.export:
        rows = tr.list(status=args.status, source=args.source, limit=args.limit)
        if args.export == "md":
            lines = ["# Pending Items\n", ""]
            for r in rows:
                if not isinstance(r, dict):
                    lines.append("## (malformed record — non-dict JSONL line, left untouched)")
                    continue
                lines.append(f"## [{r.get('id', '?')}] {r.get('title', '(untitled — partial record)')}")
                lines.append(f"- source: {r.get('source', '-')}  |  status: {r.get('status', '-')}")
                lines.append(f"- score: {r.get('score', 0)}  (impact {r.get('impact', 0)} × urgency {r.get('urgency', 0)} / effort {r.get('effort', 0) + 1})")
                lines.append(f"- action: {r.get('action_type', '-')} → {r.get('action_target') or '-'}")
                lines.append(f"- due: {r['due'] or '-'}")
                lines.append(f"- draft: {r['draft'][:200]}{'...' if len(r['draft']) > 200 else ''}")
                lines.append("")
            print("\n".join(lines))
        elif args.export == "json":
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                print("No pending items.")
                return 0
            print(f"{'ID':<12} {'Source':<18} {'Status':<12} {'Score':<6} Title")
            print("-" * 90)
            for r in rows:
                print(f"{r['id']:<12} {r['source']:<18} {r['status']:<12} {r['score']:<6} {r['title']}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

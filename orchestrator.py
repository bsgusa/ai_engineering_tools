#!/usr/bin/env python3
"""Orchestrator — master loop for the AI Engineering Toolkit.

Scan sources → score pending items → draft first actions → (optionally) execute.

By default this is dry-run: it scans, scores, drafts, and writes to the local
pending tracker, but sends/commits/trades nothing.

Usage:
  python3 orchestrator.py --dry-run                    # scan everything, write drafts only
  python3 orchestrator.py --execute                    # same + attempt approved actions
  python3 orchestrator.py --execute --approve-all      # execute every drafted action
  python3 orchestrator.py --source trading_ledger      # scan one source only
  python3 orchestrator.py --report                     # write a weekly/daily report md
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

# Local imports (same-dir, stdlib-friendly like your trading stack)
sys.path.insert(0, os.path.dirname(__file__))
from pending_tracker import PendingTracker, STATUSES, ACTION_TYPES
from ai_drafts import load_config, draft

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.yaml")

EXPAND_USER = lambda p: os.path.expanduser(p) if p else p


def load_cfg(path=CONFIG_FILE):
    cfg = {
        "paths": {
            "trading_dir": "~/trading",
            "job_automation_dir": "~/AI-Job-Automation",
            "n8n_dir": "~/.n8n",
            "state_dir": "./state",
            "reports_dir": "./reports",
            "ledger_file": "./state/pending.jsonl",
        },
        "sources": {
            "trading_ledger": True,
            "trading_reports": True,
            "blofin_account": True,
            "job_automation": True,
            "inbox_files": False,
            "inbox_path": "~/notes",
        },
        "trading": {
            "ledger_file": "~/trading/ledger.jsonl",
            "env_file": "~/trading/.env",
        },
        "scoring": {
            "impact_default": 3,
            "urgency_default": 3,
            "effort_default": 3,
        },
        "safety": {
            "dry_run_by_default": True,
            "require_execute_flag": True,
            "max_auto_actions_per_run": 0,
        },
    }
    if yaml and os.path.exists(path):
        try:
            with open(path) as fh:
                disk = yaml.safe_load(fh) or {}
            cfg.update(disk)
        except Exception:
            pass
    return cfg


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# =================================================================== sources
class Source:
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    def scan(self, tracker):
        """Yield pending-item dicts to insert into the tracker."""
        raise NotImplementedError


class TradingLedgerSource(Source):
    """Scan ~/trading/ledger.jsonl for rejected decisions, risk events, drift."""

    name = "trading_ledger"

    def scan(self, tracker):
        ledger = EXPAND_USER(self.cfg["trading"]["ledger_file"])
        if not os.path.exists(ledger):
            return []
        items = []
        try:
            with open(ledger) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = rec.get("kind", "")
                    payload = rec.get("payload", {}) or {}
                    at = rec.get("at", "")

                    # Rejected decision → needs human review
                    if kind == "decision" and payload.get("status") == "REJECTED":
                        failures = "; ".join(payload.get("failures", []) or [])
                        items.append({
                            "source": self.name,
                            "title": f"Rejected trade signal: {payload.get('instId', '?')}",
                            "body": (f"Signal {payload.get('instId')} {payload.get('side')} "
                                     f"was rejected by RiskGate. Reason: {failures}\n"
                                     f"Ledger at: {at}"),
                            "impact": 4, "urgency": 4, "effort": 2,
                            "action_type": "review", "action_target": payload.get("instId", ""),
                            "metadata": {"ledger_at": at, "mode": payload.get("mode", "paper"),
                                         "failures": payload.get("failures", [])},
                        })

                    # Risk event → review
                    elif kind == "risk_event":
                        sev = payload.get("severity", "warn")
                        impact = 5 if sev == "critical" else (4 if sev == "error" else 3)
                        items.append({
                            "source": self.name,
                            "title": f"Risk event: {payload.get('kind', 'unknown')}",
                            "body": (f"Risk gate event `{payload.get('kind')}` severity={sev} "
                                     f"at {at}.\nDetail: {json.dumps(payload, default=str)}"),
                            "impact": impact, "urgency": 4, "effort": 2,
                            "action_type": "review", "action_target": "",
                            "metadata": {"ledger_at": at, "severity": sev},
                        })

                    # Drift → serious, needs reconciliation
                    elif kind == "emergency_close":
                        items.append({
                            "source": self.name,
                            "title": "Emergency close-all fired",
                            "body": (f"A close-all was executed at {at}. "
                                     f"Review positions and reconcile ledger vs exchange.\n"
                                     f"{json.dumps(payload, default=str)}"),
                            "impact": 5, "urgency": 5, "effort": 3,
                            "action_type": "review", "action_target": "",
                            "metadata": {"ledger_at": at},
                        })
        except OSError:
            pass
        return items


class TradingReportsSource(Source):
    """Check ~/trading/reports/ for stale or missing daily reports."""

    name = "trading_reports"

    def scan(self, tracker):
        reports_dir = EXPAND_USER(os.path.join(self.cfg["paths"]["trading_dir"], "reports"))
        items = []
        if not os.path.isdir(reports_dir):
            items.append({
                "source": self.name,
                "title": "Trading reports directory missing",
                "body": f"Expected reports dir at {reports_dir} but it does not exist. "
                        f"Run python3 daily_report.py --write to generate one.",
                "impact": 3, "urgency": 2, "effort": 1,
                "action_type": "report", "action_target": reports_dir,
                "metadata": {"path": reports_dir},
            })
            return items

        try:
            files = sorted(f for f in os.listdir(reports_dir) if f.endswith(".md"))
        except OSError:
            files = []

        if not files:
            items.append({
                "source": self.name,
                "title": "No daily trading reports found",
                "body": f"No report markdown files in {reports_dir}. Generate one with:\n"
                        f"  python3 daily_report.py --write",
                "impact": 3, "urgency": 2, "effort": 1,
                "action_type": "report", "action_target": reports_dir,
                "metadata": {"path": reports_dir},
            })
            return items

        # Flag the most recent report if it's older than 2 days
        latest = files[-1]
        latest_path = os.path.join(reports_dir, latest)
        try:
            mtime = os.path.getmtime(latest_path)
            age_days = (time.time() - mtime) / 86400
            if age_days > 2:
                items.append({
                    "source": self.name,
                    "title": f"Stale daily report: {latest}",
                    "body": (f"Latest report `{latest}` is {age_days:.1f} days old. "
                             f"Run python3 daily_report.py --write to refresh."),
                    "impact": 2, "urgency": 2, "effort": 1,
                    "action_type": "report", "action_target": latest_path,
                    "metadata": {"path": latest_path, "age_days": round(age_days, 1)},
                })
        except OSError:
            pass
        return items


class BloFinAccountSource(Source):
    """Check ~/trading/.env for missing credentials that block live trading."""

    name = "blofin_account"

    def scan(self, tracker):
        env_file = EXPAND_USER(self.cfg["trading"]["env_file"])
        items = []
        if not os.path.exists(env_file):
            items.append({
                "source": self.name,
                "title": "BloFin .env missing — live orders locked",
                "body": (f"No .env at {env_file}. Create it with BLOFIN_API_KEY, "
                         f"BLOFIN_API_SECRET, BLOFIN_API_PASSPHRASE, and Supabase creds."),
                "impact": 5, "urgency": 5, "effort": 1,
                "action_type": "review", "action_target": env_file,
                "metadata": {"path": env_file},
            })
            return items

        try:
            with open(env_file) as fh:
                content = fh.read()
        except OSError:
            return items

        missing = []
        for key in ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_API_PASSPHRASE",
                    "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
            if key not in content or f"{key}=" not in content:
                missing.append(key)

        if "BLOFIN_API_PASSPHRASE" not in content or "BLOFIN_API_PASSPHRASE=" not in content:
            if "BLOFIN_API_PASSPHRASE" not in missing:
                missing.append("BLOFIN_API_PASSPHRASE")

        live_locked = "TRADING_MODE=live" not in content

        if missing:
            items.append({
                "source": self.name,
                "title": "Missing BloFin credentials: " + ", ".join(missing),
                "body": (f"The following are not set in {env_file}: {', '.join(missing)}.\n"
                         f"Live order placement is locked until all three BloFin credentials "
                         f"are present and SPOT permission is enabled on the key.\n"
                         f"See ~/trading/ACTION_PLAN.md §2."),
                "impact": 5, "urgency": 5, "effort": 1,
                "action_type": "review", "action_target": env_file,
                "metadata": {"path": env_file, "missing": missing},
            })

        if live_locked:
            items.append({
                "source": self.name,
                "title": "TRADING_MODE not set to live — execution locked",
                "body": ("Set TRADING_MODE=live and TRADING_CONFIRM=I_UNDERSTAND_REAL_MONEY "
                         f"in {env_file} only after all §6 decision gates pass "
                         "(see ~/trading/ACTION_PLAN.md)."),
                "impact": 4, "urgency": 3, "effort": 1,
                "action_type": "review", "action_target": env_file,
                "metadata": {"path": env_file},
            })
        return items


class JobAutomationSource(Source):
    """Placeholder scanner for the job-automation stack (Lexora/Supabase).

    Points at your repo so you can add Supabase queries later. For now it
    offers a manual entry reminder.
    """

    name = "job_automation"

    def scan(self, tracker):
        repo = EXPAND_USER(self.cfg["paths"]["job_automation_dir"])
        items = []
        if not os.path.isdir(repo):
            return items
        # Hook: you can add Supabase reads here to pull applications with
        # status in (applied, interviewing) and last_activity > N days.
        items.append({
            "source": self.name,
            "title": "Review job-automation pending applications",
            "body": (f"Check {repo} for job applications awaiting follow-up. "
                     f"Add a Supabase query here to auto-detect stale applications."),
            "impact": 3, "urgency": 2, "effort": 2,
            "action_type": "review", "action_target": repo,
            "metadata": {"path": repo},
        })
        return items


class InboxFilesSource(Source):
    """Scan a folder of .txt/.md files for TODO/PENDING markers."""

    name = "inbox_files"

    def scan(self, tracker):
        folder = EXPAND_USER(self.cfg["sources"]["inbox_path"])
        items = []
        if not self.cfg["sources"].get("inbox_files") or not os.path.isdir(folder):
            return items
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                if not fn.endswith((".txt", ".md")):
                    continue
                path = os.path.join(root, fn)
                try:
                    with open(path) as fh:
                        for lineno, line in enumerate(fh, 1):
                            stripped = line.strip()
                            if any(marker in stripped.upper() for marker in ("TODO:", "PENDING:", "FIXME:")):
                                items.append({
                                    "source": self.name,
                                    "title": f"Todo in {fn}:{lineno}",
                                    "body": (f"{path}:{lineno} — {stripped}"),
                                    "impact": 3, "urgency": 3, "effort": 2,
                                    "action_type": "review", "action_target": path,
                                    "metadata": {"path": path, "line": lineno},
                                })
                except OSError:
                    pass
        return items


SOURCES = {
    "trading_ledger": TradingLedgerSource,
    "trading_reports": TradingReportsSource,
    "blofin_account": BloFinAccountSource,
    "job_automation": JobAutomationSource,
    "inbox_files": InboxFilesSource,
}


# =================================================================== actions
class ActionExecutor:
    """Executes drafted actions. Dry-run by default; --execute required."""

    def __init__(self, cfg, execute=False, approve_all=False):
        self.cfg = cfg
        self.execute = execute
        self.approve_all = approve_all
        self.safety = cfg.get("safety", {})
        self.count = 0

    def run(self, item, draft_text):
        """Decide whether to execute an action for an item. Returns a result dict."""
        action_type = item.get("action_type", "review")
        target = item.get("action_target", "")

        result = {
            "id": item["id"],
            "title": item["title"],
            "action_type": action_type,
            "target": target,
            "executed": False,
            "skipped": False,
            "reason": "",
            "output": "",
        }

        max_auto = self.safety.get("max_auto_actions_per_run", 0)
        if max_auto and self.count >= max_auto:
            result["skipped"] = True
            result["reason"] = "max_auto_actions_per_run reached"
            return result

        if not self.execute:
            result["skipped"] = True
            result["reason"] = "dry-run (pass --execute to actually act)"
            return result

        if not self.approve_all:
            # Interactive approval (only when a tty is available)
            if sys.stdin.isatty():
                print(f"\n--- Action: {item['title']} ({action_type}) ---")
                print(f"Target: {target or '-'}")
                print(f"Draft:\n{draft_text[:400]}{'...' if len(draft_text) > 400 else ''}")
                ans = input("Execute? [y/N]: ").strip().lower()
                if ans not in ("y", "yes"):
                    result["skipped"] = True
                    result["reason"] = "user declined"
                    return result
            else:
                result["skipped"] = True
                result["reason"] = "not a tty and --approve-all not set"
                return result

        # ---- actual execution hooks (safe, limited)
        try:
            if action_type == "email":
                result = self._exec_email(item, draft_text, result)
            elif action_type == "slack":
                result = self._exec_slack(item, draft_text, result)
            elif action_type == "report":
                result = self._exec_report(item, draft_text, result)
            elif action_type == "commit":
                result = self._exec_commit(item, draft_text, result)
            elif action_type == "trade":
                result = self._exec_trade(item, draft_text, result)
            elif action_type == "review":
                result["executed"] = False
                result["reason"] = "review-only (no auto action)"
            else:
                result["skipped"] = True
                result["reason"] = f"unknown action type: {action_type}"
        except Exception as exc:  # never crash the orchestrator on one action
            result["executed"] = False
            result["reason"] = f"error: {exc}"

        if result["executed"]:
            self.count += 1
        return result

    def _exec_email(self, item, draft_text, result):
        """Hook: integrate with your mail client (mailto, sendgrid, etc.)."""
        result["executed"] = False
        result["reason"] = "email hook not wired — copy draft to your mail client"
        result["output"] = draft_text
        return result

    def _exec_slack(self, item, draft_text, result):
        """Hook: Slack webhook if you configure one."""
        result["executed"] = False
        result["reason"] = "slack hook not wired — copy draft to Slack"
        result["output"] = draft_text
        return result

    def _exec_report(self, item, draft_text, result):
        """Write a report markdown file to reports/."""
        reports_dir = Path(EXPAND_USER(self.cfg["paths"]["reports_dir"]))
        reports_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in item["title"])
        path = reports_dir / f"{time.strftime('%Y%m%d')}-{safe_name}.md"
        path.write_text(draft_text + "\n")
        result["executed"] = True
        result["reason"] = "report written"
        result["output"] = str(path)
        return result

    def _exec_commit(self, item, draft_text, result):
        """Hook: draft a commit. Does not commit automatically — too dangerous."""
        result["executed"] = False
        result["reason"] = "commit hook not wired — copy draft message to git"
        result["output"] = draft_text
        return result

    def _exec_trade(self, item, draft_text, result):
        """Trade hook. Locked unless your trading .env has the two-key unlock."""
        env_file = EXPAND_USER(self.cfg["trading"]["env_file"])
        live = False
        if os.path.exists(env_file):
            try:
                with open(env_file) as fh:
                    content = fh.read()
                live = ("TRADING_MODE=live" in content and
                        "TRADING_CONFIRM=I_UNDERSTAND_REAL_MONEY" in content)
            except OSError:
                pass
        if not live:
            result["executed"] = False
            result["reason"] = "trade locked — TRADING_MODE not live (see ACTION_PLAN.md §6)"
        else:
            result["executed"] = False
            result["reason"] = "trade auto-execution disabled — place manually via executor.py"
        result["output"] = draft_text
        return result


# =================================================================== orchestrator
class Orchestrator:
    def __init__(self, cfg, execute=False, approve_all=False, sources=None, report=False):
        self.cfg = cfg
        self.execute = execute
        self.approve_all = approve_all
        self.report = report
        self.tracker = PendingTracker(
            ledger_file=EXPAND_USER(cfg["paths"]["ledger_file"]),
        )
        self.actions = ActionExecutor(cfg, execute=execute, approve_all=approve_all)
        selected = sources or [k for k, v in cfg.get("sources", {}).items() if v]
        self.selected = [s for s in selected if s in SOURCES]

    def run(self):
        all_items = []
        for src_name in self.selected:
            src_cls = SOURCES[src_name]
            try:
                found = src_cls(self.cfg).scan(self.tracker)
            except Exception as exc:  # one bad source never kills the run
                print(f"  source {src_name} failed: {exc}", file=sys.stderr)
                found = []
            for item in found:
                # score defaults
                item.setdefault("impact", self.cfg["scoring"]["impact_default"])
                item.setdefault("urgency", self.cfg["scoring"]["urgency_default"])
                item.setdefault("effort", self.cfg["scoring"]["effort_default"])
                item["score"] = PendingTracker.score(item["impact"], item["urgency"], item["effort"])
                # draft
                item["draft"] = draft(
                    item["action_type"], item["title"], item["body"],
                    item.get("action_target", ""), self.cfg,
                )
                item["status"] = "open"
                item["created_at"] = iso_now()
                item["updated_at"] = iso_now()
                all_items.append(item)

        # Insert into tracker (dedupe by title+source within this run is handled by tracker IDs)
        inserted = []
        for item in all_items:
            rec = self.tracker.add(
                source=item["source"],
                title=item["title"],
                body=item["body"],
                impact=item["impact"], urgency=item["urgency"], effort=item["effort"],
                suggested_action=item["action_type"],
                draft=item["draft"],
                action_type=item["action_type"],
                action_target=item.get("action_target", ""),
                due=item.get("due", ""),
                metadata=item.get("metadata", {}),
            )
            inserted.append(rec)

        # Execute actions
        results = []
        for rec in inserted:
            res = self.actions.run(rec, rec["draft"])
            results.append(res)

        # Optional report
        if self.report:
            self._write_report(inserted, results)

        return {"inserted": inserted, "results": results}

    def _write_report(self, inserted, results):
        reports_dir = Path(EXPAND_USER(self.cfg["paths"]["reports_dir"]))
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"orchestrator-{time.strftime('%Y-%m-%d')}.md"
        lines = [f"# Orchestrator run — {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
        lines.append(f"Mode: {'EXECUTE' if self.execute else 'DRY-RUN'}")
        lines.append(f"Sources: {', '.join(self.selected)}")
        lines.append(f"New pending items: {len(inserted)}")
        lines.append(f"Actions executed: {sum(1 for r in results if r['executed'])}")
        lines.append(f"Actions skipped: {sum(1 for r in results if r['skipped'])}")
        lines.append("")
        lines.append("## New pending items (by score)")
        for r in sorted(inserted, key=lambda x: x["score"], reverse=True):
            lines.append(f"- [{r['id']}] {r['title']} — score {r['score']} ({r['source']})")
        lines.append("")
        lines.append("## Action results")
        for res in results:
            mark = "✓" if res["executed"] else ("⊘" if res["skipped"] else "○")
            lines.append(f"- {mark} {res['id']} {res['title']} — {res['reason']}")
        path.write_text("\n".join(lines) + "\n")
        print(f"Report written: {path}")


def main():
    ap = argparse.ArgumentParser(description="AI Engineering Toolkit orchestrator")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="scan and draft only (default)")
    ap.add_argument("--execute", action="store_true",
                    help="actually execute approved actions")
    ap.add_argument("--approve-all", action="store_true",
                    help="execute every drafted action without asking")
    ap.add_argument("--source", action="append", help="scan only this source (repeatable)")
    ap.add_argument("--report", action="store_true", help="write a run report to reports/")
    ap.add_argument("--config", default=CONFIG_FILE, help="path to config.yaml")
    args = ap.parse_args()

    # Safety: --execute overrides --dry-run; require --execute for any action
    execute = args.execute and not args.dry_run
    if args.execute and args.dry_run:
        print("Note: --execute overrides --dry-run.", file=sys.stderr)

    cfg = load_cfg(args.config)
    orch = Orchestrator(
        cfg, execute=execute, approve_all=args.approve_all,
        sources=args.source, report=args.report,
    )
    out = orch.run()

    print(f"\nOrchestrator complete. {len(out['inserted'])} new pending item(s).")
    for r in out["results"]:
        mark = "✓" if r["executed"] else ("⊘" if r["skipped"] else "○")
        print(f"  {mark} {r['id']} {r['title'][:60]} — {r['reason']}")

    if execute:
        print("\nTo review pending items: python3 pending_tracker.py --list")
    else:
        print("\nDry-run only. Pass --execute to act (and --approve-all to skip prompts).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

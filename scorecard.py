#!/usr/bin/env python3
"""Daily 6-line scorecard — money first. Created 2026-11-07.

Answers one question honestly: did money move since yesterday?

Design rules:
  - Every line is DERIVED from a real file or command. Nothing hardcoded as
    success; nothing counted on vibes. If a source is missing the line says
    "unavailable", never a guessed number.
  - Counts only. This script never reads, prints, or stores a prospect name,
    email, phone, or note — pipeline.csv is reduced to a row count and a stage
    histogram, and the raw file is never echoed. PII stays out of every artifact.
  - Writes a dated snapshot to state/scorecard-YYYY-MM-DD.txt so "changed
    since yesterday" is a diff of two real files, not memory.

Usage:
  python3 scorecard.py            # print to stdout
  python3 scorecard.py --write    # print + save snapshot
"""

import argparse
import csv
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
ROOT = Path(os.path.expanduser("~"))
PIPELINE = ROOT / "Documents" / "lexora-private" / "pipeline.csv"
DECISIONS = ROOT / "DECISION_SHEET.md"
ATTORNEY = ROOT / "AI-Job-Automation" / "docs" / "ATTORNEY_BRIEF.md"
LEDGER = STATE / "pending.jsonl"

STAGE_TARGET = 10          # MONEY_LANE Week-1: 10 qualified conversations
BOXES_TOTAL = 5            # ATTORNEY_BRIEF §8 sign-off checklist
LOOP_TARGET = 4            # COMPUTE_LANE Pattern B: 4 proposed loops


def safe(fn, default="unavailable"):
    try:
        v = fn()
        return default if v is None else v
    except Exception as exc:                     # noqa: BLE001 - report, never crash
        return f"unavailable ({type(exc).__name__})"


# ---------------------------------------------------------------- line 1/6
def money():
    """Revenue today. Sourced from the published pricing/billing state."""
    idx = ROOT / "AI-Job-Automation" / "index.html"
    if not idx.exists():
        return "$? — index.html missing", ""
    html = idx.read_text(errors="replace")
    # The site is authoritative and says plainly that it is not charging.
    not_charging = bool(re.search(r"not\s+yet\s+charging", html, re.I))
    if not_charging:
        return "$0", 'site: "Planned Pricing. Not Yet Charging." (index.html)'
    # Only claim a figure if a charging marker is genuinely present.
    return "$? — site no longer says 'not yet charging'", (
        "confirm billing state before trusting any number"
    )


# ---------------------------------------------------------------- line 2/6
def conversations():
    """Week-1 outreach: rows logged, stages, and how many lack a next date.

    Wrapped explicitly: launchd agents lack macOS TCC permission for
    ~/Documents, so opening the pipeline from a scheduled run raises
    PermissionError (live-verified 2026-11-07, exit status 1). The scorecard
    must degrade to an explained line, never crash — a dead scorecard reports
    nothing, and a silent one reports lies.
    """
    if not PIPELINE.exists():
        return 0, STAGE_TARGET, "pipeline.csv MISSING", []
    try:
        with PIPELINE.open(newline="") as fh:
            rows = [r for r in csv.DictReader(fh)
                    if any((v or "").strip() for v in r.values())]
    except PermissionError:
        return None, STAGE_TARGET, (
            "blocked: launchd lacks Full Disk Access to ~/Documents "
            "(System Settings > Privacy & Security > Full Disk Access)"
        ), []
    except OSError as exc:
        return None, STAGE_TARGET, f"unreadable ({type(exc).__name__})", []
    stages, undated = {}, 0
    for r in rows:
        s = (r.get("stage") or "?").strip() or "?"
        stages[s] = stages.get(s, 0) + 1
        if not (r.get("next_date") or "").strip():
            undated += 1
    note = (f"{undated} missing next_date" if undated
            else ("none logged yet" if not rows else "every row dated"))
    return len(rows), STAGE_TARGET, note, sorted(stages.items(), key=lambda x: -x[1])


# ---------------------------------------------------------------- line 3/6
def attorney_boxes():
    """§8 checklist: count checked boxes in the sign-off section only."""
    if not ATTORNEY.exists():
        return 0, "ATTORNEY_BRIEF.md missing"
    text = ATTORNEY.read_text(errors="replace")
    head = re.search(r"^##\s*8\.", text, re.M)
    if not head:
        return 0, "section 8 not found"
    section = text[head.start():]
    nxt = re.search(r"^##\s+", section[4:], re.M)
    if nxt:
        section = section[:nxt.start() + 4]
    done = len(re.findall(r"^\s*[-*]\s*\[x\]", section, re.I | re.M))
    total = done + len(re.findall(r"^\s*[-*]\s*\[ \]", section, re.M))
    return done, f"of {total or BOXES_TOTAL}"


# ---------------------------------------------------------------- line 4/6
def scheduled_loops():
    """Compute-lane loops only (COMPUTE_LANE Pattern B), actually loaded.

    Deliberately excludes com.balamonis.dealdesk — a running business service,
    not one of the 4 proposed automation loops. Counting it would have reported
    3/4 instead of the true 2/4.
    """
    lane = {"com.balamonis.daily-triage", "com.balamonis.weekly-review"}
    pending = {"com.balamonis.intro-nudges", "com.balamonis.n8n"}   # not yet scheduled
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:                                    # noqa: BLE001
        return None, "launchctl unavailable"
    loaded, healthy = set(), set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] in lane:
            loaded.add(parts[-1])
            if parts[1] == "0":                          # exit status 0 = no errors
                healthy.add(parts[-1])
    note = f"{len(healthy)} healthy, {len(pending - loaded)} not yet scheduled"
    return len(healthy), note


# ---------------------------------------------------------------- line 5/6
def decisions():
    """Open owner decisions D1-D6 in DECISION_SHEET.md."""
    if not DECISIONS.exists():
        return None, "DECISION_SHEET.md missing"
    text = DECISIONS.read_text(errors="replace")
    ids = sorted(set(re.findall(r"\bD([1-9])\b", text)))
    return len(ids), "D" + ", D".join(ids) if ids else "none found"


# ---------------------------------------------------------------- line 6/6
def changed_since_yesterday(snapshot_text):
    """Diff today's six lines against the most recent prior snapshot."""
    if not STATE.exists():
        STATE.mkdir(exist_ok=True)
    prior = sorted(STATE.glob("scorecard-*.txt"))
    today_name = f"scorecard-{dt.date.today().isoformat()}.txt"
    prior = [p for p in prior if p.name != today_name]
    if not prior:
        return "no prior snapshot — first run"
    old = prior[-1].read_text(errors="replace").splitlines()
    new = snapshot_text.splitlines()
    diffs = [f"  - {n.strip()}" for n, o in zip(new, old)
             if n.strip() and n.strip() != o.strip()]
    if not diffs:
        return f"unchanged since {prior[-1].stem.replace('scorecard-', '')}"
    label = prior[-1].stem.replace("scorecard-", "")
    return f"vs {label}:\n" + "\n".join(diffs[:8])


def build():
    m_label, m_src = money()
    c_now, c_tgt, c_note, stages = conversations()
    a_done, a_note = attorney_boxes()
    l_healthy, l_note = scheduled_loops()
    d_open, d_note = decisions()

    hdr = dt.date.today().strftime("%A %d %b %Y")
    c_label = "unavailable" if c_now is None else f"{c_now}/{c_tgt}"
    lines = [
        f"SCORECARD — {hdr}",
        "=" * 62,
        f"1  REVENUE            {m_label}",
        f"     {m_src}",
        f"2  CONVERSATIONS      {c_label}   {c_note}",
        f"     (Money Lane Week-1 target — the metric that matters)",
    ]
    if stages:
        lines.append("     stages: " + ", ".join(f"{s}={n}" for s, n in stages))
    lines += [
        f"3  ATTORNEY BOXES     {a_done}/{BOXES_TOTAL}   {a_note}",
        f"4  SCHEDULED LOOPS    {'unavailable' if l_healthy is None else f'{l_healthy}/{LOOP_TARGET}'}   {l_note}",
        f"5  OPEN DECISIONS     {d_open if d_open is not None else 'unavailable'}   {d_note}",
        "=" * 62,
    ]

    body = "\n".join(lines)
    tail = changed_since_yesterday(body)
    body += f"\n6  CHANGED            {tail}\n"
    body += "\nRule: lines 1-3 are money. Lines 4-5 are motion. "
    body += "Do not confuse them.\n"
    return body


def main():
    ap = argparse.ArgumentParser(description="Daily 6-line scorecard")
    ap.add_argument("--write", action="store_true", help="save state/scorecard-<date>.txt")
    args = ap.parse_args()

    text = build()
    print(text)

    if args.write:
        STATE.mkdir(exist_ok=True)
        path = STATE / f"scorecard-{dt.date.today().isoformat()}.txt"
        # Strip the trailing "changed" section: snapshots must store only the
        # six measured lines, or yesterday's diff would compare against its own
        # diff and report noise.
        head = "\n".join(text.split("6  CHANGED")[0].splitlines()) + "\n"

        # Never persist a partial baseline (live-verified 2026-11-07: the
        # launchd run cannot read ~/Documents, so line 2 came back
        # "unavailable"). Saving that would make tomorrow's diff compare a real
        # count against the word "unavailable" — noise presented as change on
        # the most important line. Missing data is not a measurement.
        if "2  CONVERSATIONS      unavailable" in head:
            print("snapshot: SKIPPED — line 2 unavailable, baseline would be "
                  "partial. Interactive run will write it.")
        else:
            path.write_text(head)
            print(f"snapshot: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

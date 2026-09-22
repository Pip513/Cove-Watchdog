"""Phase 1 - work out the session-state semantics.

The question this answers: can we tell a backup that is legitimately running
right now from one wedged in InProcess for days? Both report status=InProcess,
so status alone is not enough.

Candidate signals, per datasource:
  F00  last session status
  F09  last SUCCESSFUL session timestamp
  F12  session duration
  F15  last session timestamp
  F16  last successful session status
  F17  last completed session status
  F18  last completed session timestamp

Compares the Total datasource (D09) against Files (D01) and System State (D02)
to check whether Total masks a single stuck plugin.

Run:  python explore_sessions.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from cove import CoveClient, CoveCredentials, CoveError

FIELDS = {
    "F00": "last session status",
    "F09": "last SUCCESS ts",
    "F12": "session duration",
    "F15": "last session ts",
    "F16": "last success status",
    "F17": "last completed status",
    "F18": "last completed ts",
}

SOURCES = {"D09": "Total", "D01": "Files", "D02": "SystemState"}

CONTEXT = {"I1": "name", "I18": "computer", "I32": "os type", "I8": "customer"}

SESSION_STATUS = {
    "1": "InProcess",
    "2": "Failed",
    "3": "Aborted",
    "5": "Completed",
    "6": "Interrupted",
    "7": "NotStarted",
    "8": "CompletedWithErrors",
    "9": "InProgressWithFaults",
    "10": "OverQuota",
    "11": "NoSelection",
    "12": "Restarted",
}

TIME_FIELDS = {"F09", "F15", "F18"}
STATUS_FIELDS = {"F00", "F16", "F17"}


def flatten(device: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in device.get("Settings") or []:
        if isinstance(entry, dict):
            out.update(entry)
    return out


def show(code: str, raw: str | None) -> str:
    if raw in (None, ""):
        return "-"
    field = code[3:] if len(code) > 3 else code
    if field in TIME_FIELDS:
        try:
            ts = int(raw)
        except ValueError:
            return f"?{raw}"
        if ts <= 0:
            return "never"
        when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        return f"{when}  ({(time.time() - ts) / 3600:,.1f}h ago)"
    if field in STATUS_FIELDS:
        return f"{SESSION_STATUS.get(raw, raw)} ({raw})"
    if field == "F12":
        try:
            return f"{int(raw):,}s ({int(raw) / 3600:.2f}h)"
        except ValueError:
            return raw
    return raw


def main() -> int:
    load_dotenv()
    creds = CoveCredentials(
        partner=os.getenv("COVE_PARTNER", ""),
        username=os.getenv("COVE_USERNAME", ""),
        password=os.getenv("COVE_PASSWORD", ""),
        endpoint=os.getenv("COVE_ENDPOINT", "https://api.backup.management/jsonapi"),
    )

    columns = list(CONTEXT)
    for src in SOURCES:
        for field in FIELDS:
            columns.append(f"{src}{field}")

    try:
        client = CoveClient(creds)
        client.login()
        devices = client.call(
            "EnumerateAccountStatistics",
            {
                "query": {
                    "PartnerId": client.partner_id,
                    "StartRecordNumber": 0,
                    "RecordsCount": 500,
                    "SelectionMode": "Merged",
                    "Columns": columns,
                }
            },
        )
    except CoveError as exc:
        print(f"[FAIL] {exc}")
        return 1

    seen: set[str] = set()

    for device in devices or []:
        s = flatten(device)
        seen.update(s)
        if s.get("I32") == "0":
            continue  # M365 tenants, not machines

        label = "SERVER" if s.get("I32") == "2" else "workstation"
        name = s.get("I1") or s.get("I18") or "(unnamed)"
        print(f"== {name}  [{label}]  {s.get('I8') or ''}")

        for src, src_label in SOURCES.items():
            row = {f: s.get(f"{src}{f}") for f in FIELDS}
            if not any(v not in (None, "") for v in row.values()):
                continue
            print(f"   {src} {src_label}")
            for field, desc in FIELDS.items():
                value = row[field]
                if value in (None, ""):
                    continue
                print(f"      {field} {desc:<24} {show(src + field, value)}")
        print()

    print("-" * 68)
    missing = [c for c in columns if c not in seen]
    if missing:
        print("Columns never returned for any device (check for bad codes):")
        print("  " + ", ".join(missing))
    else:
        print("All requested columns returned for at least one device.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

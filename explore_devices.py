"""Phase 1 - enumerate devices and inspect the columns the watchdog depends on.

Read-only. Answers the questions that block detection logic:
  - does EnumerateAccountStatistics recurse into child customers?
  - does D09F09 (last successful backup, any datasource) actually populate?
  - is I32 (OS type) reliable for telling servers from workstations?
  - do profile names come through on this role?
  - which requested columns come back empty (they fail silently)

Run:  python explore_devices.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from cove import CoveClient, CoveError
from cove.runner import credentials_from_env

# Columns the watchdog needs, plus context for reading the output.
COLUMNS: dict[str, str] = {
    "I0": "device id",
    "I1": "device name",
    "I18": "computer name",
    "I8": "customer",
    "I32": "os type",
    "I16": "os version",
    "I81": "physicality",
    "I78": "active datasources",
    "I4": "created",
    "I54": "profile id",
    "I56": "profile",
    "I14": "used storage",
    "D09F09": "last successful backup (Total)",
    "D09F15": "last session (Total)",
    "D09F00": "last session status (Total)",
}

OS_TYPE = {"0": "undefined", "1": "workstation", "2": "SERVER"}

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

PAGE_SIZE = 500


def flatten(device: dict) -> dict[str, str]:
    """Settings is a list of single-key objects; collapse it to a dict."""
    out: dict[str, str] = {}
    for entry in device.get("Settings") or []:
        if isinstance(entry, dict):
            out.update(entry)
    return out


def fmt_epoch(raw: str | None) -> tuple[str, float | None]:
    """Return (human readable, age in hours) for a Unix timestamp string."""
    if not raw:
        return "never", None
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return f"unparseable({raw!r})", None
    if ts <= 0:
        return "never", None
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    age_h = (time.time() - ts) / 3600.0
    return when.strftime("%Y-%m-%d %H:%M UTC"), age_h


def fetch_all(client: CoveClient, partner_id: int) -> list[dict]:
    """Page through every device visible to this partner."""
    devices: list[dict] = []
    start = 0
    while True:
        result = client.call(
            "EnumerateAccountStatistics",
            {
                "query": {
                    "PartnerId": partner_id,
                    "StartRecordNumber": start,
                    "RecordsCount": PAGE_SIZE,
                    "SelectionMode": "Merged",
                    "Columns": list(COLUMNS),
                }
            },
        )
        batch = result or []
        devices.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return devices


def main() -> int:
    load_dotenv()
    creds = credentials_from_env()

    try:
        client = CoveClient(creds)
        client.login()
        devices = fetch_all(client, client.partner_id)
    except CoveError as exc:
        print(f"[FAIL] {exc}")
        return 1

    print(f"Devices visible to partner {client.partner_id}: {len(devices)}\n")
    if not devices:
        print("No devices returned. If you expected some, the query may not be")
        print("recursing into child customers - try a child partner id directly.")
        return 1

    # --- per device ---------------------------------------------------------
    partners: dict[int, int] = {}
    os_types: dict[str, int] = {}
    seen_columns: set[str] = set()

    for device in devices:
        s = flatten(device)
        seen_columns.update(s)
        pid = device.get("PartnerId")
        partners[pid] = partners.get(pid, 0) + 1
        os_types[s.get("I32", "(absent)")] = os_types.get(s.get("I32", "(absent)"), 0) + 1

        name = s.get("I1") or s.get("I18") or "(unnamed)"
        os_label = OS_TYPE.get(s.get("I32", ""), f"?({s.get('I32')})")
        last_ok, age_h = fmt_epoch(s.get("D09F09"))
        last_any, _ = fmt_epoch(s.get("D09F15"))
        status = SESSION_STATUS.get(s.get("D09F00", ""), s.get("D09F00") or "-")
        created, _ = fmt_epoch(s.get("I4"))

        age_note = "never succeeded" if age_h is None else f"{age_h:,.1f}h ago"
        flag = ""
        if os_label == "SERVER":
            flag = "  <<< STALE" if (age_h is None or age_h > 4) else "  ok"

        print(f"  {name}")
        print(f"     account id     {device.get('AccountId')}   partner {pid}")
        print(f"     customer       {s.get('I8') or '-'}")
        print(f"     os             {os_label}  |  {s.get('I16') or '-'}")
        print(f"     physicality    {s.get('I81') or '-'}")
        print(f"     computer name  {s.get('I18') or '-'}")
        print(f"     datasources    {s.get('I78') or '-'}")
        print(f"     profile        {s.get('I56') or '-'} (id {s.get('I54') or '-'})")
        print(f"     created        {created}")
        print(f"     last success   {last_ok}  ({age_note}){flag}")
        print(f"     last session   {last_any}  status={status}")
        print()

    # --- aggregates ---------------------------------------------------------
    print("-" * 64)
    print("Devices per partner id:")
    for pid, count in sorted(partners.items()):
        print(f"  {pid}: {count}")
    if len(partners) > 1:
        print("  -> the query DOES recurse into child customers")
    else:
        print("  -> only one partner id seen; recursion unconfirmed")

    print("\nOS type distribution (I32):")
    for value, count in sorted(os_types.items()):
        print(f"  {OS_TYPE.get(value, value):<12} {count}")

    # --- the silent-failure check ------------------------------------------
    print("\nColumn integrity (codes that fail silently if malformed):")
    missing = [c for c in COLUMNS if c not in seen_columns]
    if missing:
        for code in missing:
            print(f"  [!] {code:<8} {COLUMNS[code]:<32} never returned for any device")
        print("\n  A column absent across ALL devices usually means a bad code,")
        print("  not absent data. Verify before trusting it in detection logic.")
    else:
        print("  all requested columns returned for at least one device")

    return 0


if __name__ == "__main__":
    sys.exit(main())

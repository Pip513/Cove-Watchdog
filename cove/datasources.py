"""Cove datasource codes and session-status values.

Datasource codes are 3 characters with mandatory zero padding (D01, never D1).
Malformed codes are ignored silently by the API, so these are defined once here
rather than written inline at call sites.
"""

from __future__ import annotations

# Datasource code -> human name. Source: N-able column codes documentation.
DATASOURCES: dict[str, str] = {
    "D01": "Files and Folders",
    "D02": "System State",
    "D03": "MsSql (deprecated)",
    "D04": "Exchange (VSS)",
    "D05": "Microsoft 365 SharePoint",
    "D06": "Network Shares",
    "D07": "System State (VSS)",
    "D08": "VMware Virtual Machines",
    "D09": "Total",
    "D10": "MsSql (VSS)",
    "D11": "SharePoint (VSS)",
    "D12": "Oracle",
    "D14": "Hyper-V",
    "D15": "MySql",
    "D16": "Virtual Disaster Recovery",
    "D17": "Bare Metal Restore",
    "D19": "Microsoft 365 Exchange",
    "D20": "Microsoft 365 OneDrive",
    "D23": "Microsoft 365 Teams",
}

# "Total" is an aggregate, not a real datasource. It reports the MOST RECENT
# success across all sources, so a healthy plugin masks a dead one. Never
# evaluate staleness against it.
AGGREGATE_CODE = "D09"

# Statistics field codes used by the watchdog.
FIELD_LAST_SUCCESS = "F09"  # last successful session timestamp
FIELD_LAST_STATUS = "F00"  # last session status
FIELD_LAST_SESSION = "F15"  # last session ts; empty while a session is in flight

SESSION_STATUS: dict[str, str] = {
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

# I32 - OS type
OS_UNDEFINED = 0  # in practice, a Microsoft 365 tenant rather than a machine
OS_WORKSTATION = 1
OS_SERVER = 2


def parse_active_datasources(raw: str | None) -> list[str]:
    """Split an I78 value into datasource codes.

    I78 concatenates 3-character codes in fixed ascending order, e.g.
    "D01D02D10" -> ["D01", "D02", "D10"]. Unknown chunks are kept so that a new
    Cove datasource surfaces as an unrecognised code rather than vanishing.
    """
    if not raw:
        return []
    return [raw[i : i + 3] for i in range(0, len(raw) - len(raw) % 3, 3)]


def datasource_name(code: str) -> str:
    return DATASOURCES.get(code, f"Unknown datasource {code}")


def status_name(raw: str | None) -> str:
    if raw in (None, ""):
        return "unknown"
    return SESSION_STATUS.get(str(raw), f"status {raw}")

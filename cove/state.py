"""Durable state between runs.

The check is stateless in itself: each run asks Cove what is true now. State
exists only to answer questions about *history* that no single run can:

  - have we already told someone about this device today?
  - was this device failing last time, so that recovering is news?
  - when did this outage actually start?
  - has the weekly report gone out yet this week?

Two backends. `JsonFileStateStore` for local runs and tests;
`TableStorageStateStore` for Azure, importing the SDK lazily so the package does
not require it everywhere.

Everything is stored as UTC. A state file written in one timezone and read in
another must not shift.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

#: Meta keys, kept as constants because a typo would silently reset a schedule.
META_REPORT_LAST_SENT = "report_last_sent"
META_FAILURE_LAST_SENT = "failure_last_sent"
META_FAILURE_FIRST_AT = "failure_first_at"
META_FAILURE_KIND = "failure_kind"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("Discarding unparseable stored timestamp %r", raw)
        return None
    # Tolerate a naive value written by an older version rather than crashing.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class DeviceState:
    """What we remember about a device that is currently failing.

    A device with no entry is, by definition, considered healthy. Recovery is
    therefore "had an entry, no longer failing" and the entry is deleted.
    """

    account_id: int
    first_detected: datetime
    last_alerted: datetime
    #: Datasource codes failing at the last alert. Used to notice the problem
    #: spreading to a source that was healthy when we last wrote.
    failing_sources: list[str] = field(default_factory=list)
    label: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["first_detected"] = _iso(self.first_detected)
        data["last_alerted"] = _iso(self.last_alerted)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DeviceState":
        return cls(
            account_id=int(data["account_id"]),
            first_detected=_parse(data.get("first_detected")) or datetime.now(timezone.utc),
            last_alerted=_parse(data.get("last_alerted")) or datetime.now(timezone.utc),
            failing_sources=list(data.get("failing_sources") or []),
            label=data.get("label") or "",
        )


class StateStore(Protocol):
    """Minimal interface the dispatcher needs."""

    def get_device(self, account_id: int) -> DeviceState | None: ...
    def put_device(self, state: DeviceState) -> None: ...
    def delete_device(self, account_id: int) -> None: ...
    def all_devices(self) -> list[DeviceState]: ...
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str | None) -> None: ...
    def commit(self) -> None: ...

    # Convenience wrappers for the common timestamp case.
    def get_meta_time(self, key: str) -> datetime | None: ...
    def set_meta_time(self, key: str, value: datetime | None) -> None: ...


class _MetaTimeMixin:
    def get_meta_time(self, key: str) -> datetime | None:
        return _parse(self.get_meta(key))  # type: ignore[attr-defined]

    def set_meta_time(self, key: str, value: datetime | None) -> None:
        self.set_meta(key, _iso(value))  # type: ignore[attr-defined]


class InMemoryStateStore(_MetaTimeMixin):
    """For tests, and for a dry run that must not persist anything."""

    def __init__(self) -> None:
        self._devices: dict[int, DeviceState] = {}
        self._meta: dict[str, str] = {}

    def get_device(self, account_id: int) -> DeviceState | None:
        return self._devices.get(account_id)

    def put_device(self, state: DeviceState) -> None:
        self._devices[state.account_id] = state

    def delete_device(self, account_id: int) -> None:
        self._devices.pop(account_id, None)

    def all_devices(self) -> list[DeviceState]:
        return list(self._devices.values())

    def get_meta(self, key: str) -> str | None:
        return self._meta.get(key)

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._meta.pop(key, None)
        else:
            self._meta[key] = value

    def commit(self) -> None:
        return None


class JsonFileStateStore(_MetaTimeMixin):
    """A single JSON file. Fine for one instance; not for concurrent writers.

    Loaded once on construction and written on commit, so a crash mid-run
    leaves the previous state intact rather than a half-updated one.
    """

    def __init__(self, path: str | Path = "watchdog_state.json") -> None:
        self.path = Path(path)
        self._devices: dict[int, DeviceState] = {}
        self._meta: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Losing state means re-alerting once, which is noisy but safe.
            # Refusing to run because a state file is corrupt would be worse.
            log.warning("Could not read state from %s (%s); starting empty", self.path, exc)
            return
        for entry in data.get("devices") or []:
            try:
                state = DeviceState.from_dict(entry)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Discarding malformed device state %r (%s)", entry, exc)
                continue
            self._devices[state.account_id] = state
        self._meta = {k: v for k, v in (data.get("meta") or {}).items() if v is not None}

    def get_device(self, account_id: int) -> DeviceState | None:
        return self._devices.get(account_id)

    def put_device(self, state: DeviceState) -> None:
        self._devices[state.account_id] = state

    def delete_device(self, account_id: int) -> None:
        self._devices.pop(account_id, None)

    def all_devices(self) -> list[DeviceState]:
        return list(self._devices.values())

    def get_meta(self, key: str) -> str | None:
        return self._meta.get(key)

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._meta.pop(key, None)
        else:
            self._meta[key] = value

    def commit(self) -> None:
        payload = {
            "version": 1,
            "devices": [s.to_dict() for s in self._devices.values()],
            "meta": self._meta,
        }
        # Write to a sibling temp file and replace, so an interrupted write
        # cannot leave a truncated state file behind.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def default_store() -> StateStore:
    """Pick a backend from the environment.

    Azure Table Storage when a connection string is configured, otherwise a
    local JSON file. The Table Storage backend arrives with the Function host;
    until then this always returns the file store.
    """
    path = os.getenv("WATCHDOG_STATE_PATH", "watchdog_state.json")
    return JsonFileStateStore(path)

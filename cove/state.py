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

from .env import env_str

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


class TableStorageStateStore(_MetaTimeMixin):
    """Azure Table Storage, for the Function host.

    Devices and meta live in one table under two partitions. The SDK is
    imported lazily so local runs and tests need no Azure dependency.

    Reads happen once, on first access. Writes are deferred to commit() and
    only touch entities that actually changed, so a run over a large fleet
    that alters nothing costs one query and no writes.
    """

    DEVICE_PARTITION = "device"
    META_PARTITION = "meta"

    def __init__(
        self,
        *,
        table_name: str = "covewatchdog",
        connection_string: str | None = None,
        account_url: str | None = None,
    ) -> None:
        self.table_name = table_name
        self._connection_string = connection_string
        self._account_url = account_url

        self._devices: dict[int, DeviceState] = {}
        self._meta: dict[str, str] = {}
        self._dirty_devices: set[int] = set()
        self._deleted_devices: set[int] = set()
        self._dirty_meta: set[str] = set()
        self._loaded = False
        self._client = None

    def _table(self):
        if self._client is not None:
            return self._client

        from azure.data.tables import TableServiceClient  # lazy: Azure only

        if self._connection_string:
            service = TableServiceClient.from_connection_string(
                self._connection_string
            )
        elif self._account_url:
            # Managed identity - no secret to store or rotate.
            from azure.identity import DefaultAzureCredential

            service = TableServiceClient(
                endpoint=self._account_url, credential=DefaultAzureCredential()
            )
        else:
            raise ValueError(
                "TableStorageStateStore needs either a connection string or an "
                "account URL."
            )

        service.create_table_if_not_exists(self.table_name)
        self._client = service.get_table_client(self.table_name)
        return self._client

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for entity in self._table().list_entities():
            partition = entity.get("PartitionKey")
            if partition == self.DEVICE_PARTITION:
                try:
                    self._devices[int(entity["RowKey"])] = DeviceState(
                        account_id=int(entity["RowKey"]),
                        first_detected=_parse(entity.get("first_detected"))
                        or datetime.now(timezone.utc),
                        last_alerted=_parse(entity.get("last_alerted"))
                        or datetime.now(timezone.utc),
                        failing_sources=json.loads(
                            entity.get("failing_sources") or "[]"
                        ),
                        label=entity.get("label") or "",
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("Discarding malformed device row %r (%s)", entity, exc)
            elif partition == self.META_PARTITION:
                value = entity.get("value")
                if value is not None:
                    self._meta[entity["RowKey"]] = value

    def get_device(self, account_id: int) -> DeviceState | None:
        self._load()
        return self._devices.get(account_id)

    def put_device(self, state: DeviceState) -> None:
        self._load()
        self._devices[state.account_id] = state
        self._dirty_devices.add(state.account_id)
        self._deleted_devices.discard(state.account_id)

    def delete_device(self, account_id: int) -> None:
        self._load()
        if self._devices.pop(account_id, None) is not None:
            self._deleted_devices.add(account_id)
        self._dirty_devices.discard(account_id)

    def all_devices(self) -> list[DeviceState]:
        self._load()
        return list(self._devices.values())

    def get_meta(self, key: str) -> str | None:
        self._load()
        return self._meta.get(key)

    def set_meta(self, key: str, value: str | None) -> None:
        self._load()
        if value is None:
            self._meta.pop(key, None)
        else:
            self._meta[key] = value
        self._dirty_meta.add(key)

    def commit(self) -> None:
        table = self._table()

        for account_id in self._dirty_devices:
            state = self._devices[account_id]
            table.upsert_entity(
                {
                    "PartitionKey": self.DEVICE_PARTITION,
                    "RowKey": str(account_id),
                    "first_detected": _iso(state.first_detected),
                    "last_alerted": _iso(state.last_alerted),
                    "failing_sources": json.dumps(state.failing_sources),
                    "label": state.label,
                }
            )

        for account_id in self._deleted_devices:
            try:
                table.delete_entity(self.DEVICE_PARTITION, str(account_id))
            except Exception as exc:  # already gone is not an error
                log.debug("Could not delete device row %s: %s", account_id, exc)

        for key in self._dirty_meta:
            value = self._meta.get(key)
            if value is None:
                try:
                    table.delete_entity(self.META_PARTITION, key)
                except Exception as exc:
                    log.debug("Could not delete meta row %s: %s", key, exc)
            else:
                table.upsert_entity(
                    {
                        "PartitionKey": self.META_PARTITION,
                        "RowKey": key,
                        "value": value,
                    }
                )

        self._dirty_devices.clear()
        self._deleted_devices.clear()
        self._dirty_meta.clear()


def default_store() -> StateStore:
    """Pick a backend from the environment.

    Azure Table Storage when this is running in a Function App, otherwise a
    local JSON file. Managed identity is preferred over a connection string
    when both are available, so there is one fewer secret to rotate.
    """
    account_url = env_str("WATCHDOG_TABLE_ACCOUNT_URL")
    connection_string = (
        env_str("WATCHDOG_STATE_CONNECTION")
        or os.getenv("AzureWebJobsStorage", "").strip()
    )
    table_name = env_str("WATCHDOG_TABLE_NAME", "covewatchdog")

    if account_url or connection_string:
        return TableStorageStateStore(
            table_name=table_name,
            # Prefer identity; fall back to the connection string.
            connection_string=None if account_url else connection_string,
            account_url=account_url or None,
        )

    path = env_str("WATCHDOG_STATE_PATH", "watchdog_state.json")
    return JsonFileStateStore(path)

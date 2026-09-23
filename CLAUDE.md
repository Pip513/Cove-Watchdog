# Cove missed-backup watchdog

A monitoring tool for **N-able Cove Data Protection** that alerts on backups
which *never ran*.

## Why this exists

Cove alerts on backups that **failed** or **completed with errors**. It does not
alert on backups that simply **did not happen**. A device that stops checking in,
a scheduled job that silently stops firing, or a session that wedges in
`InProcess` for weeks produces no failure event — and therefore no notification.
From the console it can look fine.

This tool closes that gap: it polls the Cove API, works out how long ago each
backup data source last *succeeded*, and emails when that exceeds a threshold.

A second, less obvious reason it exists: an external watchdog does not depend on
the system it watches. If Cove's own alerting is having a bad day, its alerting
about having a bad day is not to be relied on.

---

## Design principles

Four rules drive most of the decisions in this codebase. If you change
something and it seems oddly defensive, it is probably one of these.

### 1. Silence must never be mistaken for health

This is a monitoring tool. The failure mode that matters is not "sends a wrong
alert" — it is "sends nothing, and nobody notices it stopped". Everything below
follows from that.

- If the fleet's state cannot be established, **no device alerts are sent at
  all**, and a failure email goes out instead.
- If the check itself breaks, that is emailed as its own event.
- A scheduled report is sent on a fixed slot **whether or not anything is
  wrong**, so its absence is itself a signal.

### 2. Never evaluate against the aggregate

Cove exposes a `D09` "Total" data source. It reports the **most recent** success
across all of a device's data sources, not the oldest. A server whose file
backup runs hourly therefore looks healthy even when its SQL backup has been
dead for weeks.

Evaluation is **per data source**, always. `cove/datasources.py` defines
`AGGREGATE_CODE = "D09"` and `build_columns()` explicitly excludes it.

### 3. The API fails silently, so verify everything

Cove ignores unrecognised or malformed column codes **without returning an
error** — no error, no data. For this tool that is lethal: a typo in a column
code is indistinguishable from "this device has never backed up", and would
alert on the entire fleet at once.

Every response is therefore integrity-checked before it is trusted, and the
check refuses to report rather than emit a false all-clear.

### 4. Configuration goes stale, so verify that too

Profile names are matched **exactly**, which means a profile renamed in the
console matches nothing — and monitoring nothing looks exactly like everything
being healthy. Configured profile names are checked against the profiles that
actually exist on devices, and an unmatched monitor profile is a hard failure.

---

## How detection works

Per run (intended hourly):

1. `Login` to the Cove API, receive a **visa** (session token, valid 15 minutes).
2. `EnumerateAccountStatistics` for the authenticated partner. This **recurses
   into child customers**, so one call covers the whole tree.
3. For each device, read its active data sources from column `I78`
   (e.g. `"D01D02D10"` → Files, System State, SQL).
4. For each active source, read `D##F09` — last **successful** session timestamp.
5. Alert if `now - last_success > threshold`, evaluated **per source**.

### Scope rules, applied in order

| Order | Rule | Skip reason |
|---|---|---|
| 1 | On an ignore profile | `IGNORED_PROFILE` |
| 2 | Monitor profile set and device does not match | `NOT_MONITORED` |
| 2b | No monitor profile set, and device is not a server (`I32 != 2`) | `NOT_A_SERVER` |
| 3 | Created less than the grace period ago | `IN_GRACE_PERIOD` |
| 4 | No active data sources | `NO_DATASOURCES` |

Muting always wins. When `WATCHDOG_MONITOR_PROFILE` is set it **replaces** the
server test, so a workstation profile can be monitored deliberately.

### Why stuck sessions need no special rule

A session wedged in `InProcess` for weeks is caught by the ordinary threshold,
because `F09` (last success) keeps its value independently of what is currently
running:

- **Healthy, mid-backup** — a session is running, but the previous hourly run
  succeeded, so `F09` is recent. No alert. Correct.
- **Wedged** — a session is running and never succeeds, so `F09` freezes and
  ages out. Alert. Correct.

Keying on last-success rather than session status is deliberate: it catches
*every* way a backup fails to produce data, not just the wedged flavour.

---

## Files

### Package: `cove/`

#### `cove/client.py`
JSON-RPC transport. `CoveClient` handles login, the visa lifecycle, retries and
error surfacing. `CoveCredentials` holds connection settings; its `password`
field is `repr=False` so tokens stay out of tracebacks and logs.

Things worth knowing before editing:

- **Errors return HTTP 200.** Cove signals failure only in the response body.
  Anything branching on status codes reads every failure — including auth
  failure — as success. `_raise_for_rpc_error` inspects the `error` key.
- Error shape is `{"error": {"code": -32603, "data": 2100, "message": "..."}}`.
  `code` is generic JSON-RPC; **`data` carries the Cove-specific code** and is
  the one worth branching on. `2100` means bad partner/username/password.
- The visa lasts 15 minutes and is refreshed at 13, so no call can go out on a
  visa that expires mid-flight. Every response's visa is adopted — including
  error responses — so a failed call does not break the chain.
- Retries apply only to transport faults and 5xx, never to an API-level error.

#### `cove/errors.py`
Exception hierarchy. The distinction that matters:

- `CoveApiError` — the API returned an error.
- `CoveAuthError` — subclass of the above; **must be tested first** when
  classifying, or a bad token reads as a generic API error.
- `CoveDataError` — the API reported *success* but the response cannot be
  trusted. Separate on purpose: it is the silent-failure case.
- `CoveConfigError`, `CoveTransportError` — configuration and network.

#### `cove/datasources.py`
Cove's column vocabulary, defined once rather than written inline at call sites
(malformed codes fail silently, so scattering string literals is dangerous).

- `DATASOURCES` — `D01` Files, `D02` System State, `D10` SQL (VSS), etc.
- `AGGREGATE_CODE = "D09"` — the Total aggregate, never evaluated.
- `FIELD_LAST_SUCCESS = "F09"`, `FIELD_LAST_STATUS = "F00"`,
  `FIELD_LAST_SESSION = "F15"`.
- `parse_active_datasources()` — splits `I78` into 3-character codes.
- `SESSION_STATUS` — the status enum (`1` InProcess, `5` Completed, …).

**Zero-padding is mandatory**: `D01F09` works, `D1F9` returns nothing, silently.

#### `cove/devices.py`
Fetching and modelling. `fetch_devices()` pages through
`EnumerateAccountStatistics`, flattens the awkward response shape, and builds
`Device` / `DatasourceStatus` objects.

The response nests as `result.result[]`, each entry carrying `AccountId`,
`PartnerId`, `Flags` at top level and requested columns in a `Settings` array of
**single-key objects** — hence `_flatten()`.

Integrity checks, both raising `CoveDataError`:
- Every device must return the `CRITICAL_COLUMNS` (`I1`, `I32`, `I78`, `I4`).
- At least one device must return a last-success column — otherwise the column
  codes are wrong, not the fleet dead.

`build_columns()` requests every real data source up front, because a device's
active sources cannot be known before fetching it. Only the sources each
device's own `I78` reports are evaluated.

Note `in_flight`: `F15` comes back empty **exactly while a session is running**
on that source. An odd encoding, but a reliable "currently running" signal.

#### `cove/detection.py`
The rules. `Config` holds thresholds, profile lists and timezone;
`evaluate_device()` applies the scope rules and produces a `DeviceResult`
containing a `DatasourceFinding` per source with a verdict of `OK`, `STALE` or
`NEVER`.

`verify_scope()` is the config-staleness guard: it compares configured profile
names against those actually in use. An unmatched **monitor** profile raises
(monitoring nothing looks like health); an unmatched **ignore** profile only
warns (the consequence is noise, not silence).

Profile matching is **exact**, case- and whitespace-insensitive. Substring
matching was rejected because fleets commonly have profiles like `X` and
`X Server` — matching the former as a substring silently captures the latter.

`Config.validate()` rejects an unresolvable timezone rather than degrading to
UTC, because the daily re-alert fires at a local wall-clock hour and a silent
fallback would send at the wrong time without complaint.

#### `cove/messages.py`
Alert and recovery email bodies. Plain text, written for a technician triaging
at 8am: what is broken, how long, and enough identifiers to act.

Deliberate choices:
- **Plain text only.** Renders identically everywhere and survives ticket-system
  ingestion without markup artefacts.
- **Two lines per failing source.** Outlook and most mobile clients hard-wrap
  plain text near 78 characters; a single line carrying name, timestamp, age and
  status ran to 96 and wrapped exactly where the table mattered. Longest line is
  now 76.
- **Both names.** Cove holds two: `I18` the machine hostname (used in the
  subject and header, what a technician recognises) and `I1` the Cove account
  name (in the footer, what you search the console by). Both are needed.
- **Relative and absolute times.** The age answers "how bad", the timestamp
  answers "when did it actually stop".
- **No hard-coded times.** The next-reminder line is computed from
  `WATCHDOG_REALERT_HOUR` and `WATCHDOG_TIMEZONE` via `dispatch.next_daily_send`,
  with a real date and zone. It also says a reminder may come sooner, because
  a failure spreading to another data source alerts at once.
- **Stable subjects** per device and per kind, so a ticket system threading on
  subject groups repeat alerts into one ticket rather than opening a new one
  daily.

#### `cove/report.py`
The scheduled heartbeat report. Sent on a fixed weekly slot regardless of state
and independently of the alert path — a report that only arrives when there is
news cannot tell you the watchdog has died.

`is_report_due()` assumes an hourly check: it fires on the first run at or after
the configured local hour on the configured day, and `last_sent` stops it
repeating. A missed run still sends later the same day rather than skipping the
week.

The body lists the most recently backed-up devices as evidence of life, plus one
line naming the device **closest to the threshold** — early warning without a
second list.

#### `cove/notify.py`
SMTP delivery. Provider-agnostic: a hosted sender, or an internal relay that
accepts unauthenticated mail on a plain connection. `security` covers
`starttls` / `ssl` / `none`, and auth is skipped entirely when no username is
set.

Certificate verification is **off by default** (`SMTP_VERIFY_CERT=false`), a
deliberate choice because some relays present certificates that fail it. The
connection is still encrypted, but the server is not authenticated, so an
interceptor could collect the SMTP password. Anyone on a provider with a valid
certificate should turn it on.

Port 25 is blocked outbound on most Azure compute; a connection failure on that
port says so explicitly rather than leaving someone to debug a timeout.

#### `cove/health.py`
Failures of the check itself. `classify()` maps an exception to a `FailureKind`;
each kind carries remediation text that goes into the email, so the fix travels
with the alert.

`CheckFailure.alerts_suppressed` encodes rule 1: every kind suppresses device
alerts **except** `EMAIL`, because in that case the alerts were real and only
delivery broke.

`should_send_failure()` sends the first failure immediately — a blind watchdog
should be reported as soon as it goes blind, not next morning — then at most
daily. A week-long outage produces 7 emails, not 168.

#### `cove/runner.py`
One run, with every failure path accounted for. `run_check()` **never raises**;
it returns a `CheckOutcome`. An unhandled exception in an hourly job is
indistinguishable from a run that found nothing wrong.

`CheckOutcome.alerting` returns empty whenever a failure is present. That
emptiness is load-bearing: on failure the fleet's state is unknown, and staying
silent about devices is the only honest option.

Config is validated before any API call — a stale profile name makes everything
after it meaningless and costs a round trip to discover otherwise.

#### `cove/env.py`
Every setting is read through here, so one rule holds everywhere: **blank means
the default**, exactly as if the setting were absent. Clearing a value is how
people "reset" a setting in the portal, and reading blank as an empty string
used to break that three ways — numbers failed to parse, a blank
`REPORT_ENABLED` silently turned the weekly report off, and a blank
`WATCHDOG_SCHEDULE` left the timer with no schedule so the Function never
loaded.

`env_str`, `env_int`, `env_float`, `env_bool` and `env_secret` (credentials,
never stripped). A value that is set but malformed raises `CoveConfigError`
naming the setting. The single exception, `ALERT_SUBJECT_PREFIX`, passes
`blank_is_empty=True` because blank is a real choice there: no prefix.

**Never read a setting with `os.getenv` directly.** It bypasses this rule, and
`test_settings.py` fails if you do. `AzureWebJobsStorage` is the one raw read,
because it belongs to Azure rather than to this project.

#### `cove/state.py`
Durable state between runs. The check is stateless in itself; state exists only
to answer questions about *history* that no single run can - have we already
told someone today, was this device failing last time, when did the outage
start, has the weekly report gone out.

Two backends behind one protocol: `JsonFileStateStore` for local runs and tests,
`TableStorageStateStore` for Azure. The Azure SDK is imported **lazily**, so
local runs and tests pull in no Azure dependency. Everything is stored as UTC.

The file store writes to a temp file and `os.replace`s it, so an interrupted
write cannot truncate state. A corrupt state file logs and starts empty rather
than crashing - losing state costs one duplicate alert, refusing to run costs
you the monitoring.

Table Storage defers writes to `commit()` and touches only changed entities, so
an hourly run over a healthy fleet costs one query and no writes.

#### `cove/dispatch.py`
Compares what is true now against what was true last run, and produces the
messages that follow. Every cadence rule lives here and is testable without a
mail server or an API.

`plan()` decides and reads state; `commit()` records. They are separate so that
a send failure does not mark a message as delivered.

`next_daily_send()` sits beside `is_daily_due()` and computes the time an
alert email promises for the next reminder. They are kept together, and tested
against each other, because the email once hard-coded "08:00 tomorrow" and
would have kept saying so whatever `WATCHDOG_REALERT_HOUR` was set to.

#### `cove/deliver.py`
Renders a plan into messages and sends them. `plan_for_delivered()` reduces a
plan to what actually sent, so an undelivered alert is retried next run instead
of being remembered as delivered. One failed recipient does not stop the rest of
the batch.

Above `max_emails_per_run`, individual alerts collapse into one summary naming
every device. A site-wide outage - or a bug here - should not open a hundred
tickets.

### Scripts

All are read-only against Cove. **None of them send email unless explicitly
told to.**

#### `check_auth.py`
Verifies credentials. Logs in, reports user/partner/role, checks account posture
flags, proves the visa chain with a second call, and resolves the role ID to its
name. Run this first — it isolates credential problems from everything else.

#### `explore_devices.py`
Enumerates devices and prints the columns detection depends on, with an
integrity check listing any requested column that never came back. Useful when
onboarding a new tenant or debugging why a device is not being picked up.

#### `explore_sessions.py`
Diagnostic for session semantics — compares `F00`/`F09`/`F12`/`F15`/`F16`/`F17`/
`F18` across Total, Files and System State. This is the script that established
that Total reports the *newest* success across sources, and that `F15` empties
while a session runs. Keep it for when Cove changes something.

#### `check_backups.py`
The main dry run. Applies the full rules and prints exactly which alerts would
fire and why. Sends nothing.

```
python check_backups.py            # alerts only
python check_backups.py --all      # include healthy and skipped devices
python check_backups.py --report   # also render the weekly report
```

Also flags monitored devices not on an hourly profile — a server nobody
configured for hourly backups is its own silent problem.

#### `run_watchdog.py`
The watchdog itself: one complete run. `function_app.py` calls its `execute()`,
so the hosted and local paths cannot drift.

Defaults to a dry run - prints what it would send, writes no state. `--send` is
required to deliver anything, so an accidental invocation mails nobody.

Note that a dry run deliberately persists nothing, so you cannot rehearse the
multi-day cadence by running it repeatedly. `test_dispatch.py` covers that.

#### `function_app.py`
The Azure Functions host. Thin by design. See **The host adapter** under
Deployment for when it raises and why.

#### `preview_emails.py`
Prints every email variant with synthetic data. No SMTP settings needed. Use it
to check wording before anything fires for real.

#### `send_test_email.py`
Verifies SMTP settings end to end. Renders a realistic alert from a synthetic
stale device and, with `--send`, delivers it — so delivery can be tested without
waiting for a real failure or mailing about a healthy machine.

```
python send_test_email.py                     # render only
python send_test_email.py --send              # deliver it
python send_test_email.py --send --recovery   # the all-clear format
```

#### `test_detection.py`
41 cases covering detection rules, profile scoping and report scheduling. The
live fleet is usually healthy, so a real run never exercises the alerting path —
these do.

#### `test_failures.py`
39 cases covering failure classification, alert suppression, failure-email
suppression and content, `run_check` never raising, and SMTP config validation.

#### `test_dispatch.py`
77 cases covering the cadence, rendering, partial delivery, both state
backends, and the reminder time alert emails promise. Consecutive runs are
simulated against a real store rather than asserting on a single call, because
the rules that matter are about sequences.

#### `test_settings.py`
The parsing rules — blank means default, malformed values fail naming the
setting, the subject-prefix exception — plus the regressions they fixed. Then
the drift check: every setting the code reads must appear, with the same
default, in the setup script, `azure-app-settings.txt`, the README reference
and `.env.example`, and the README and script must agree on which are required.
It also fails if anything reads a setting with `os.getenv` directly, or if the
txt was hand-edited rather than regenerated.

Defaults come straight from the code by parsing its calls into `cove/env.py`,
so a default changed in one place fails the suite until it is changed in all
five. Verified by deliberately breaking each rule in a copy of the repository:
all seven kinds of drift are caught.

Run all four:
```
python test_detection.py && python test_failures.py && python test_dispatch.py && python test_settings.py
```

### Other files

- **`.env.example`** — annotated configuration template; doubles as setup docs.
- **`.gitignore`** — `.env` is ignored. Verify with `git check-ignore -v .env`
  after any change.
- **`requirements.txt`** — `azure-functions`, `requests`, `python-dotenv`,
  `tzdata`, and the Azure Table/identity SDKs. `tzdata` is required on Windows,
  which ships no system timezone database; without it `zoneinfo` fails and
  times silently fall back to UTC.
- **`host.json` / `.funcignore`** — Functions host configuration, and what to
  keep out of the deployment package (tests, exploration scripts, secrets,
  local state, the settings txt). Only `func azure functionapp publish` honours
  `.funcignore`; the GitHub Actions route ships the whole repository except
  `.git` and `.github`, which is harmless because nothing in it is secret.
- **`.github/deploy-workflow.example.yml`** — a corrected GitHub Actions deploy
  workflow. It does not run from there. Azure Deployment Center's generated
  Python workflow installs dependencies into the build runner instead of the
  package and zips with `./*`, which skips `.python_packages`; the result
  deploys "successfully" and then fails to load with `ModuleNotFoundError`.
  The example fixes both and adds build-time checks. Reconfiguring Deployment
  Center regenerates the broken file, which is why the fix is kept here.
- **`azure-app-settings.txt`** — every setting as a block to paste into the
  Function App's **Environment variables → Advanced edit**, with instructions
  above a `COPY BELOW THIS LINE` marker. The block begins with a comma so it
  appends to the three settings Azure creates rather than replacing them.
  Generated from the script below; see *Adding a setting*.
- **`scripts/setup-app-settings.ps1`** — the single definition of every setting
  and its default. With `-AppName`/`-ResourceGroup` it adds missing settings via
  Azure CLI and never overwrites; with `-OutputJson` it prints the paste block
  and needs no CLI. Not deployed.
- **`LICENSE`** — MIT.

**Syncing the repository does not create app settings.** Code deploys and app
settings are separate. Two attempts to automate settings on sync were built
and removed: a standalone workflow needed five secrets and variables in the
deploying fork, and a composite action hooked into Azure's generated workflow
could not work because that workflow's deploy job never checks out the
repository. Truly automatic settings need a one-time secret setup in the fork,
because GitHub does not sync secrets and Azure's secret names carry a random
suffix.

---

## Setup

### 1. Create a Cove API user

In the Management Console: **Management > Users > API Users > Add API user**.

- Requires a SuperUser or Administrator to create.
- Choose the **lowest role that can read devices, customers and backup
  profiles**. At time of writing that is `Operator`; `Supporter` is lower but
  cannot read profiles, which the ignore/monitor profile feature needs.
- Do **not** tick Security Officer — this tool never needs to generate a
  recovery passphrase.
- **The token is shown exactly once.** Copy it straight into `.env`.

An API user carries the `NonInteractive` flag and **cannot log into the
console**, which is why it is preferred over a regular user with API access.

### 2. Configure

```bash
cp .env.example .env
```

The one that catches everybody: **`COVE_PARTNER` must match the console
exactly**, and the console name usually includes the contact email in
parentheses — `Acme Ltd (admin@acme.com)`, not `Acme Ltd`. The bare company name
is rejected with "Unknown partner/username or bad password", which reads like a
credential problem and is not.

### 3. Install and verify

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python check_auth.py
.venv/bin/python check_backups.py --all
```

On Windows the interpreter is `.venv/Scripts/python.exe`.

---

## Configuration reference

The complete reference — all 31 settings with required, default and accepted
format — is in **`README.md` under *Configuration***. It is deliberately not
duplicated here: an earlier copy in this file drifted and silently lost seven
settings. Keep it in one place.

Threshold guidance: set it to roughly **4× the backup interval**. For hourly
backups, 4 hours tolerates three consecutive misses before alerting.

---

## Cove API notes

Hard-won, mostly undocumented. Endpoint `https://api.backup.management/jsonapi`,
`POST`, `Content-Type: application/json`. All times are Unix epoch **UTC**; all
sizes are bytes. Method and parameter names are **case sensitive**.

| Behaviour | Consequence |
|---|---|
| Errors return **HTTP 200** | Never branch on status codes |
| `error.data` holds the Cove code | `2100` = bad partner/username/password |
| Bad column codes fail **silently** | Verify columns came back before trusting a result |
| Zero-padding mandatory | `D01F09` yes, `D1F9` returns nothing |
| Visa lasts **15 minutes** | Chain it, or re-login each run |
| Enumeration **recurses** into child customers | One call covers the whole tree |
| `I32` OS type | `0` undefined (in practice a Microsoft 365 tenant), `1` workstation, `2` server |
| `D09` Total is the **newest** success | Never evaluate staleness against it |
| `F15` is empty while a session runs | Reliable "currently running" signal |
| Custom roles exist | Undocumented; the roles list is extensible beyond the five system roles |

Useful columns: `I0` device ID, `I1` device name, `I4` created, `I8` customer,
`I16` OS version, `I18` computer name, `I32` OS type, `I54`/`I56` profile ID and
name, `I78` active data sources, `I81` physical/virtual.

---

## Status

| Phase | State |
|---|---|
| 1. API exploration | Done |
| 2. Detection | Done, per data source |
| 3. Email transport and content | Done |
| 4. State store and alert cadence | Done |
| 5. Azure Functions host | Done; running on Flex Consumption, Python 3.14 |

### Cadence, now that state exists

- First detection alerts immediately, on whichever run catches it.
- A still-failing device alerts once a day at `WATCHDOG_REALERT_HOUR`, so the
  repeat lands at a predictable time rather than whenever the outage ticks over.
- A failure that **spreads** to another data source alerts at once: the scope of
  the problem has changed, so it is new information. A source recovering while
  others still fail stays silent.
- Recovery sends one all-clear, then nothing.
- A muted or deleted device is forgotten **silently**. It was not fixed, so
  claiming it recovered would be a lie, and an open ticket is the correct signal
  that a human should look.
- A failed run touches no device state at all. Treating "no devices returned" as
  "everything recovered" would clear every record during an API outage and
  re-alert the whole fleet afterwards.

A week-long outage on one device produces 8 emails, not 168.

## Deployment

Running on Flex Consumption, Python 3.14, deployed by GitHub Actions through
Azure Deployment Center's generated workflow - with the fix in
`.github/deploy-workflow.example.yml` applied, without which the Function
cannot load. `README.md` has the step-by-step. This section records the
reasoning behind the choices.

### What any host must provide

- **An hourly schedule.** Poll interval and threshold are **separate knobs**.
  Polling at the same interval as the threshold roughly doubles worst-case
  detection latency, because a device can breach just after a run and wait a
  full cycle to be noticed. With a 4-hour threshold, poll hourly.
- **Outbound HTTPS** to the Cove API and outbound SMTP to the mail host.
- **Somewhere to keep secrets** that is not the repository.
- **Somewhere durable to keep state** — see Phase 4 above.
- **An alert when the job itself fails to run.** Not when it reports a problem —
  when it does not run at all. Nothing inside this codebase can detect that.

`cove/runner.py` exists precisely so a host adapter stays thin: call
`run_check()`, act on the returned `CheckOutcome`. It never raises and never
prints, so it drops into any scheduler. The `print()`-based output lives only in
the scripts; the package logs through `logging`.

### Intended target: Azure Functions

Chosen over the alternatives for a small, stateless, scheduled job:

| Option | Why not |
|---|---|
| **Logic Apps** | Schedule and mail connectors are built in, but Cove is JSON-RPC with a session token and per-source filtering. That logic in Logic Apps expressions is painful, and Consumption billing is per-action so looping over devices costs more. |
| **Container Apps Jobs** | Fine, but a registry and image lifecycle to manage for a script. |
| **Automation runbook** | Reasonable for a PowerShell shop; worse local development story. |
| **VM with cron** | A server to patch, for a job that runs 720 times a month. |

At this volume the Flex Consumption plan's monthly free grant covers it; expect
to pay only for the backing storage account.

### The host adapter

`function_app.py` is a timer trigger that calls `run_watchdog.execute()`, so
the hosted and local paths cannot drift. `host.json` configures the host;
`local.settings.json` (gitignored) holds local secrets.

**When it raises matters**, because that is what an Application Insights
failure alert sees:

- check failed but a failure email went out -> does **not** raise. The system
  worked; a human was told. Raising would make a Cove outage look like a
  defect here and alert on something already reported.
- could not notify anyone -> **raises**. This is the case nobody would
  otherwise discover.

A timer trigger for hourly, using the six-field form of Azure's NCRONTAB
(`{second} {minute} {hour} {day} {month} {day-of-week}`):

```
0 0 * * * *
```

Azure also accepts the five-field form, and tells them apart by counting.
That makes miscounting the real trap: `0 */5 * * * *` is every five minutes,
`0 */5 * * *` every five hours.

### Azure-specific traps

These are the ones that will cost time.

**Leave the Function's timezone as UTC.** Do not set `WEBSITE_TIME_ZONE` to make
the schedule local. The code already converts to local time itself via
`WATCHDOG_TIMEZONE` and `zoneinfo`, and it is the thing that decides when the
08:00 re-alert and the weekly report fire. Two independent timezone
interpretations is a recipe for alerts arriving at the wrong hour twice a year.
Run the timer hourly in UTC and let the application reason about local time.

**Port 25 is blocked outbound** on most Azure compute. Use 587 with STARTTLS, or
2525. `cove/notify.py` says so explicitly in the error when a connection on port
25 fails, but it is better not to hit it.

**Python version decides the hosting plan.** This targets **3.14 on Flex
Consumption**, chosen for the support runway (April 2029) on something meant to
be set up once and left alone. Functions supports 3.10-3.14, but the classic
Linux Consumption plan stops at 3.12 - newer versions go only to Flex, which is
where Microsoft is investing.

The code itself uses no syntax past 3.10, so it runs across the whole supported
range; only the hosting plan constrains the choice. Verified on 3.14 with both
deprecation warnings escalated to errors.

Microsoft's own pages disagree about which versions Flex accepts, so check
`az functionapp list-runtimes --os linux` rather than trusting any of them.

**Keep `tzdata` in requirements.** Linux hosts usually ship a timezone database
and Windows never does. Including it costs nothing and removes a
platform-dependent failure that would otherwise silently shift every timestamp
to UTC. `Config.validate()` will refuse to start if the timezone cannot resolve,
which turns that silent shift into a loud failure — but only if it is missing
entirely, so do not rely on it to catch a subtler case.

**Secrets belong in Key Vault**, surfaced as Key Vault references in application
settings and read by a system-assigned managed identity. The code reads
everything through `os.getenv`, and application settings are environment
variables, so no code change is needed — `load_dotenv()` is called only in the
scripts, never in the package.

**State fits in Table Storage** in the storage account the Function already
needs. No extra resource.

**Leave the Function App's storage authentication on secrets.** Microsoft's
portal guide for Flex suggests switching the create wizard's Authentication tab
to managed identity. That replaces the `AzureWebJobsStorage` connection string
with identity-based `AzureWebJobsStorage__*` settings, which `default_store()`
does not read, so it falls back to a local JSON file that does not work in
Azure. The README tells people to leave it on secrets and add identities
afterwards through `WATCHDOG_TABLE_ACCOUNT_URL`.

**The dead-man's switch is not optional.** Configure an Application Insights
alert on function failures *and* on the absence of successful executions. This
is the layer below the weekly report: if the Function stops being invoked
entirely, no code in this repository can tell you, because none of it is
running. Budget for testing that alert — an untested dead-man's switch is
decorative.

### Deploying somewhere else

Nothing here is Azure-specific except the traps above. The requirements are an
hourly trigger, a secret store, a key-value store and an external "did this run"
alarm. A cron job with a systemd timer, a GitHub Actions scheduled workflow, or
any container scheduler works equally well. If you do fork it elsewhere, keep
the external alarm: it is the only thing standing between a dead watchdog and a
comfortable silence.

---

## Working on this

- **Run all four test suites before and after any change.** They encode failure
  modes that are not obvious from reading the code, and several exist because
  the naive version was wrong in a way that would have been silent.
- **Never introduce `D09`** into evaluation, however convenient one call looks.
- **Prefer failing loudly over degrading quietly.** If you find yourself adding
  a fallback, ask whether it could make a broken check look healthy. If so,
  raise instead.
- **Do not log or print credentials.** `CoveCredentials.password` and
  `SmtpConfig.password` are `repr=False`; keep it that way.
- When adding a check, add the matching failure kind and remediation text in
  `cove/health.py` — an alert that does not say what to do about it costs
  someone an hour.

### Adding a setting

A setting lives in five places, and missing one is how seven settings went
undocumented. In order:

1. The code — read it through a `cove/env.py` helper with a **literal**
   default, e.g. `env_int("REPORT_HOUR", 8)`. Never `os.getenv`.
2. `scripts/setup-app-settings.ps1` — the same name and default, with a
   comment.
3. `azure-app-settings.txt` — regenerate from the script's `-OutputJson`
   output; do not hand-edit the block.
4. `README.md` → *Configuration* — a row with required, default and format.
5. `.env.example` — the name, set to its default, with a comment.

**`test_settings.py` checks all five agree** — names and defaults — so a
missed step fails the suite rather than surfacing months later. It reads the
default straight from the code, which is why the default must be a literal.
A setting read only locally (like `WATCHDOG_STATE_PATH`) is declared in the
test's `LOCAL_ONLY` and skipped for the Azure-facing files.

Parsing rules to design around: blank means default everywhere except
`ALERT_SUBJECT_PREFIX`; a malformed number raises naming the setting; and
true/false settings read any unrecognised value as false, so a typo switches the
feature off.

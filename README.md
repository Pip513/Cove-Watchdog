# Cove missed-backup watchdog

Alerts on N-able Cove Data Protection backups that **never ran**.

Cove notifies you when a backup **fails**. It does not notify you when a backup
simply **does not happen** — a device that stops checking in, a schedule that
quietly stops firing, or a session wedged in `InProcess` for weeks. None of
those produce a failure event, so none of them produce an email. From the
console it can look fine.

This runs hourly, works out how long ago each backup data source last
*succeeded*, and emails when that exceeds a threshold.

**It checks each data source separately.** Cove's own "Total" figure reports the
most recent success across all of them, so a server whose file backup runs
hourly looks healthy even when its SQL backup has been dead for a fortnight.
That gap is the main reason this exists.

---

## What you get

- **Per-device alerts** naming the specific data source that stopped —
  "Microsoft SQL (VSS): last success 16.2 days ago" rather than "server is
  unhealthy".
- **A sane cadence.** One alert when a problem starts, then one a day at a fixed
  hour until it clears, then a single all-clear. A week-long outage produces 8
  emails, not 168.
- **A weekly report** sent whether or not anything is wrong. If it stops
  arriving, the watchdog itself has stopped — which a problems-only alert could
  never tell you.
- **Failure emails** when the check cannot run, explaining what broke and how to
  fix it, with device alerts deliberately suppressed because the fleet's state
  is unknown.

---

## Requirements

- An N-able Cove account and permission to create an API user
- Python **3.14** (3.10+ works; see the version note under Deploy)
- An SMTP host — a provider or an internal relay
- For deployment: an Azure subscription and Contributor rights

---

## Quick start (local, read-only)

Nothing here sends email or writes to Cove.

### 1. Create a Cove API user

In the Management Console: **Management → Users → API Users → Add API user**.

- Requires a SuperUser or Administrator to create.
- Role: the lowest that can read devices, customers **and backup profiles**. At
  time of writing that is `Operator`. `Supporter` is lower but cannot read
  profiles, which the monitor/ignore profile settings need.
- Do **not** tick Security Officer.
- **The token is shown exactly once.** Copy it straight into `.env`.

An API user carries the `NonInteractive` flag and cannot log into the console at
all, which is why it is preferred over a normal user with API access.

### 2. Configure

```bash
cp .env.example .env
```

> **The one that catches everybody:** `COVE_PARTNER` must match the console
> exactly, and the console name usually includes the contact email in
> parentheses — `Acme Ltd (admin@acme.com)`, not `Acme Ltd`. The bare company
> name is rejected with *"Unknown partner/username or bad password"*, which
> reads like a credential problem and is not.

### 3. Install and verify

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows the interpreter is `.venv/Scripts/python.exe`.

```bash
.venv/bin/python check_auth.py        # credentials and permissions
.venv/bin/python check_backups.py --all   # what it sees, and what would alert
.venv/bin/python preview_emails.py    # every email variant
.venv/bin/python run_watchdog.py      # a full run, dry
```

`run_watchdog.py` sends nothing and writes no state unless you pass `--send`.

### 4. Test email delivery

Fill in the SMTP block in `.env`, then:

```bash
.venv/bin/python send_test_email.py --send
```

That renders a realistic alert from a **synthetic** device, so you can prove
delivery without waiting for a real failure or mailing about a healthy machine.

---

## Deploy to Azure

### Python version, and why it decides your hosting plan

**This targets Python 3.14 on a Flex Consumption plan.** That combination has
the longest support runway of any current option — 3.14 is supported until
April 2029 — which suits a monitoring tool you want to set up once and leave
alone.

Azure Functions supports 3.10 through 3.14, all GA, but **the classic Linux
Consumption plan stops at 3.12**. Newer versions are only added to Flex
Consumption, and Microsoft is steering Consumption apps toward Flex. So the
version you want decides the plan you create:

| Want | Plan | End of support |
|---|---|---|
| **3.14** | **Flex Consumption** | April 2029 |
| 3.13 | Flex Consumption | October 2029 |
| 3.12 | Either | October 2028 |
| 3.11 | Either | October 2027 |
| 3.10 | Either | **October 2026** — avoid |

Nothing in this project uses syntax past 3.10, so it runs across that whole
range if you need a different version. It is verified on 3.14: the test suites
pass with deprecation warnings escalated to errors, and every dependency
imports cleanly.

**Match your local version to the deployed one.** If they differ you are not
testing what you ship.

Documentation pages disagree about which versions Flex accepts, so **ask your
own subscription rather than trusting any doc, including this one**:

```bash
az functionapp list-runtimes --os linux \
  --query "linux[?starts_with(runtime,'python')].{runtime:runtime, version:version}" -o table
```

### Prerequisites

```bash
az --version          # Azure CLI, 2.60.0 or later for Flex Consumption
func --version        # Azure Functions Core Tools v4
az login
```

### 1. Create the resources

Names for the storage account and Function App must be **globally unique**. The
storage account must be lowercase alphanumeric, 3–24 characters.

Flex Consumption is not available in every region, so pick from the supported
list:

```bash
az functionapp list-flexconsumption-locations \
  --query "sort_by(@, &name)[].{Region:name}" -o table
```

```bash
RG=rg-cove-watchdog
LOCATION=eastus          # must appear in the list above
STORAGE=covewatchdogsa$RANDOM
APP=cove-watchdog-$RANDOM
PYVER=3.14               # confirm with list-runtimes first

az group create --name $RG --location $LOCATION

az storage account create \
  --name $STORAGE \
  --resource-group $RG \
  --location $LOCATION \
  --sku Standard_LRS \
  --allow-blob-public-access false

az functionapp create \
  --name $APP \
  --resource-group $RG \
  --storage-account $STORAGE \
  --flexconsumption-location $LOCATION \
  --runtime python \
  --runtime-version $PYVER

echo "Function App: $APP"
```

<details>
<summary>Classic Consumption instead (capped at Python 3.12)</summary>

```bash
az functionapp create \
  --name $APP \
  --resource-group $RG \
  --storage-account $STORAGE \
  --consumption-plan-location $LOCATION \
  --runtime python \
  --runtime-version 3.12 \
  --functions-version 4 \
  --os-type Linux
```

Available in more regions, but it cannot go past Python 3.12 and Microsoft
documents a migration path away from it. Fine if Flex is not offered where you
need to run.
</details>

This creates an Application Insights resource automatically — you need it for
step 5.

**No table needs creating.** State lives in Azure Table Storage in the account
above, reached through the `AzureWebJobsStorage` connection string the Function
App already has, and the table is created on first use.

### 2. Configure application settings

Every setting is listed with its format in the
[configuration reference](#configuration). These must have a value before
anything runs:

| Setting | |
|---|---|
| `COVE_PARTNER` | Console name **including** any parenthesised email |
| `COVE_USERNAME` | The API user's login name |
| `COVE_PASSWORD` | The token, shown once at creation |
| `SMTP_HOST` | Your mail host |
| `ALERT_FROM` | Sender your provider allows |
| `ALERT_TO` | Where alerts go |

Plus `SMTP_USERNAME` and `SMTP_PASSWORD` if your mail provider requires
authentication — most hosted ones do.

Until these have values, **every run fails loudly**. That is the design
refusing to look healthy while it is inert, not a broken deployment.

**Deploying code does not create settings.** A deploy ships code only; app
settings are separate. Everything else has a default in the code, so it only
needs to exist in Azure if you want it visible and adjustable in the portal.
Two ways to add the full set:

**Portal, no tools needed.** Open
[`azure-app-settings.txt`](azure-app-settings.txt). It holds every setting,
with defaults filled in and the required ones blank. Then:

1. **Function App → Settings → Environment variables → Advanced edit**.
2. Put the cursor just before the final `]` at the very end of the text.
3. Paste everything below the file's `COPY BELOW THIS LINE` marker. The block
   starts with a comma so it joins the list that is already there.
4. **OK**, then **Apply**.
5. Fill in the required values listed above, then **Apply** again.

> **Advanced edit replaces everything.** Keep `AzureWebJobsStorage`,
> `APPLICATIONINSIGHTS_CONNECTION_STRING` and
> `DEPLOYMENT_STORAGE_CONNECTION_STRING` — losing them breaks the app.

`pwsh scripts/setup-app-settings.ps1 -OutputJson` prints the same block, if
you would rather generate it than copy it.

**Azure CLI.** Adds only what is missing and never overwrites, so it is safe
to re-run after pulling an update that introduces a new setting:

```bash
pwsh scripts/setup-app-settings.ps1 -AppName "<app>" -ResourceGroup "<rg>"
```

> **Do not set `WEBSITE_TIME_ZONE`.** The timer runs in UTC on purpose; the
> application converts to local time itself using `WATCHDOG_TIMEZONE` to decide
> when the daily re-alert and weekly report fire. Two independent timezone
> interpretations disagree at daylight-saving boundaries and will send at the
> wrong hour twice a year.

> **Port 25 is blocked outbound** on most Azure compute. Use 587 with STARTTLS,
> or 2525.

### 3. Deploy the code

```bash
func azure functionapp publish $APP
```

Or connect your repository under the Function App's **Deployment Center**,
which sets up a GitHub Actions workflow that deploys on every push to the
branch. That is the route that makes syncing a fork update the app.

`.funcignore` keeps tests, exploration scripts, `.env` and local state out of
the package.

### 4. Verify it runs

The timer fires hourly, which is a slow feedback loop. Speed it up temporarily:

```bash
# Run every 5 minutes (six-field NCRONTAB: seconds first)
az functionapp config appsettings set --name $APP --resource-group $RG \
  --settings "WATCHDOG_SCHEDULE=0 */5 * * * *"

az webapp log tail --name $APP --resource-group $RG
```

You should see the run logged, and — if anything is actually failing — an email.
Put it back afterwards:

```bash
az functionapp config appsettings set --name $APP --resource-group $RG \
  --settings "WATCHDOG_SCHEDULE=0 0 * * * *"
```

A good first test is to set `WATCHDOG_THRESHOLD_HOURS` to something tiny (say
`0.1`) so a real alert fires against real devices, confirm the email arrives,
then set it back to `4`.

### 5. Set up the alert that catches a dead watchdog

**This is not optional.** Everything above tells you when *backups* fail.
Nothing in this repository can tell you when the *Function* stops running,
because at that point none of it is running.

In Application Insights for your Function App, create two alert rules:

1. **Failed executions** — fires when the Function raises. It raises only when
   it could not notify anyone, so this means "a problem nobody has been told
   about".
2. **No successful executions in the last 3 hours** — fires when the Function
   has stopped being invoked at all. This is the one that catches a deleted
   Function, an expired subscription, or a broken deployment.

Point both at an address that is **not** the same inbox as the alerts, if you
can. Then test them — an untested dead-man's switch is decorative.

The weekly report is the third layer: if the report stops arriving on Monday
morning, something is wrong regardless of what any alert says.

### Cost

At hourly execution this sits well inside the Flex Consumption plan's monthly
free grant. Expect
to pay only for the storage account, which will be pennies a month. Application
Insights has a free ingestion allowance that this will not approach.

### Removing it

```bash
az group delete --name $RG --yes
```

### Optional hardening: managed identity for state

By default state is reached with the storage connection string that already
exists in the Function App. To use a managed identity instead:

1. Enable a system-assigned identity on the Function App.
2. Grant it **Storage Table Data Contributor** on the storage account.
3. Set `WATCHDOG_TABLE_ACCOUNT_URL=https://<storage>.table.core.windows.net`.

The code prefers the identity whenever that URL is set, and drops the
connection string — one fewer secret to rotate.

Cove and SMTP credentials can likewise move to Key Vault, referenced from
application settings as
`@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/<name>/)`.
No code change is needed: the application reads everything through the
environment.

---

## How it decides

Per run, for each device in scope, for **each active data source**:

| Condition | Result |
|---|---|
| Last success within `WATCHDOG_THRESHOLD_HOURS` | OK |
| Last success older than that | Alert, naming the source |
| No successful backup on record | Alert, worded differently |
| Device created less than `WATCHDOG_GRACE_HOURS` ago | Skipped — covers the initial seed |
| Device on an ignore profile | Skipped |

Scope is every server (`OS type = server`) unless `WATCHDOG_MONITOR_PROFILE` is
set, in which case it is exactly the devices on those backup profiles.

Profile names are matched **exactly**, and a name that matches nothing is a hard
failure rather than a quiet "nothing to monitor". Renaming a profile in the
console without updating the setting would otherwise leave you watching nothing
while everything looked healthy.

### A running backup is not a missed backup

The check keys on *last success*, not session status. A backup running right now
sits on top of a recent success, so it does not alert. A session wedged in
`InProcess` for weeks never produces a success, so its last-success age grows
and it does alert — through the ordinary threshold, with no special rule.

---

## Configuration

Every setting the watchdog reads. Locally they go in `.env`; in Azure they are
application settings under **Environment variables**. The names are identical.

### Format rules that catch people

- **Timezone: use a city, never an abbreviation.** `America/New_York` switches
  between EST and EDT correctly. `EST` is *accepted* but pinned to UTC−5 all
  year, so from March to November every scheduled email fires an hour late.
  Windows names like `Eastern Standard Time` are rejected outright.
  [Full list of names](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).
- **Hours are whole numbers on a 24-hour clock, in `WATCHDOG_TIMEZONE`.**
  `8` is 8 am, `17` is 5 pm. The one exception is `WATCHDOG_SCHEDULE`, which is
  UTC.
- **Durations are hours and may have decimals.** `0.5` is thirty minutes.
- **True/false accepts** `true`, `yes`, `on`, `1` — case ignored. **Anything
  else counts as false**, including typos: `REPORT_ENABLED=ture` silently turns
  the weekly report off. Check the spelling.
- **Lists are comma-separated.** Spaces around items are ignored.
- **Profile names must match the console exactly**, apart from case and
  surrounding spaces. No wildcards, no partial matches — `1 hour RPO` does not
  match `1 hour RPO Server`.
- **Blank means default.** Clearing a value in the portal resets it to the
  default, exactly as if the setting were not there. The one exception is
  `ALERT_SUBJECT_PREFIX`, where blank means *no prefix*.
- **A value that is set but malformed stops the run**, with an error naming
  the setting — `REPORT_HOUR=eight` fails rather than guessing.

### Cove API

| Setting | Required | Default | Format |
|---|---|---|---|
| `COVE_PARTNER` | **Yes** | — | Customer name exactly as the console shows it, **including** the parenthesised contact email: `Acme Ltd (admin@acme.com)` |
| `COVE_USERNAME` | **Yes** | — | The API user's login name |
| `COVE_PASSWORD` | **Yes** | — | The API token, shown once when the user is created. Secret |
| `COVE_ENDPOINT` | No | `https://api.backup.management/jsonapi` | Full URL. Only change if N-able directs you to a regional host |

### Detection

| Setting | Required | Default | Format |
|---|---|---|---|
| `WATCHDOG_THRESHOLD_HOURS` | No | `4` | Hours, decimals allowed, above 0. Checked per data source. Roughly 4× your backup interval |
| `WATCHDOG_GRACE_HOURS` | No | `24` | Hours, decimals allowed, 0 or more. How long after a device is created before it can alert |
| `WATCHDOG_MONITOR_PROFILE` | No | *(blank: all servers)* | Comma-separated profile names: `1 hour RPO Server,Daily Server`. When set, **only** these profiles are checked, workstations included. A name matching no device stops the run |
| `WATCHDOG_IGNORE_PROFILE` | No | *(blank)* | Same format. Always wins over the monitor list. A name matching no device logs a warning |
| `WATCHDOG_TIMEZONE` | No | `America/New_York` | Timezone name: `America/Chicago`, `America/Denver`, `America/Los_Angeles`, `Europe/London`, `UTC` |
| `WATCHDOG_REALERT_HOUR` | No | `8` | Whole hour, 0–23, local. When a still-failing device is re-alerted each day. The first alert is always immediate |
| `WATCHDOG_MAX_EMAILS_PER_RUN` | No | `25` | Whole number, 1 or more. Above this, one summary replaces the individual alerts |
| `WATCHDOG_SCHEDULE` | No | `0 0 * * * *` | NCRONTAB in **UTC**, five or six fields. Six fields puts seconds first. See below |

`WATCHDOG_SCHEDULE` examples:

```
0 0 * * * *       hourly, on the hour          six fields (recommended)
0 * * * *         hourly, on the hour          five fields, same result
0 30 * * * *      hourly, at half past
0 */5 * * * *     every five minutes           testing only
```

**Count the fields.** Azure reads the expression by how many there are, so the
same-looking string means different things: `0 */5 * * * *` (six) runs every
five *minutes*, while `0 */5 * * *` (five) runs every five *hours*.

### Weekly report

| Setting | Required | Default | Format |
|---|---|---|---|
| `REPORT_ENABLED` | No | `true` | True/false. Leave on: its absence on Monday is how you learn the watchdog has died |
| `REPORT_DAY` | No | `monday` | Full weekday name, case ignored: `monday` … `sunday`. Abbreviations like `mon` are rejected |
| `REPORT_HOUR` | No | `8` | Whole hour, 0–23, local |
| `REPORT_DEVICE_LIMIT` | No | `5` | Whole number, 1 or more. Devices listed before the rest collapse to a count |

### Email

| Setting | Required | Default | Format |
|---|---|---|---|
| `SMTP_HOST` | **Yes** | — | Hostname only, no `https://` and no port: `mail.smtp2go.com` |
| `SMTP_PORT` | No | `587` | Whole number. `587` for STARTTLS, `465` for SSL, `2525` as an alternate. **Not `25` on Azure** — blocked outbound |
| `SMTP_SECURITY` | No | `starttls` | `starttls`, `ssl` or `none`. Must match the port: `ssl` with 465, `starttls` with 587 or 2525. `none` only for a trusted internal relay |
| `SMTP_USERNAME` | Usually | *(blank)* | Blank means no authentication, which only an internal relay accepts |
| `SMTP_PASSWORD` | If username set | *(blank)* | Secret. Required whenever `SMTP_USERNAME` is set |
| `SMTP_VERIFY_CERT` | No | `false` | True/false. Off by default because some relays present certificates that fail verification. The connection is still encrypted, but the server's identity is not checked. Set `true` for a provider with a valid certificate, such as a hosted sender |
| `SMTP_TIMEOUT` | No | `30` | Whole number of seconds |

### Alert addressing

| Setting | Required | Default | Format |
|---|---|---|---|
| `ALERT_FROM` | **Yes** | — | One address your provider lets you send as: `backups@example.com` |
| `ALERT_FROM_NAME` | No | `Cove Backup Watchdog` | Display name shown as the sender |
| `ALERT_TO` | **Yes** | — | One or more addresses, separated by commas or semicolons: `ops@example.com, tickets@example.com` |
| `ALERT_SUBJECT_PREFIX` | No | `[Cove]` | Text placed before every subject. **Set blank for no prefix** — the only setting where blank does not mean default |

### State

| Setting | Required | Default | Format |
|---|---|---|---|
| `WATCHDOG_TABLE_NAME` | No | `covewatchdog` | Azure table name: 3–63 letters and digits, starting with a letter. Created on first use |
| `WATCHDOG_TABLE_ACCOUNT_URL` | No | *(blank)* | `https://<storage-account>.table.core.windows.net`. Setting it switches to a managed identity — see *Optional hardening* |
| `WATCHDOG_STATE_CONNECTION` | No | *(blank: uses `AzureWebJobsStorage`)* | A storage connection string, only to keep state in a different account |
| `WATCHDOG_STATE_PATH` | Local only | `watchdog_state.json` | File path for local runs. Ignored in Azure, which always uses Table Storage |

### Set by Azure, not by you

`AzureWebJobsStorage`, `APPLICATIONINSIGHTS_CONNECTION_STRING` and
`DEPLOYMENT_STORAGE_CONNECTION_STRING` are created with the Function App. Leave
them alone; state and deployment both depend on them.

---

## Development

```bash
.venv/bin/python test_detection.py    # detection rules, scoping, scheduling
.venv/bin/python test_failures.py     # failure classification and suppression
.venv/bin/python test_dispatch.py     # cadence, delivery, state
.venv/bin/python test_settings.py     # parsing rules; the five settings files agree
```

All four should pass before any change ships. They encode failure modes that
are not obvious from reading the code, and several exist because the naive
version was wrong in a way that would have been **silent**.

[`CLAUDE.md`](CLAUDE.md) explains every file, the design principles, and the
undocumented Cove API behaviour this depends on. Read it before changing
anything in `cove/`.

---

## Licence

[MIT](LICENSE). Use it, change it, ship it in something commercial — no
obligation beyond keeping the copyright notice.

It is provided **as is, without warranty**. This tool watches backups; it does
not take them, and it cannot guarantee it will catch every missed one. Treat it
as one layer of assurance, not the only one, and set up the dead-man's switch
described above so you find out when the watchdog itself stops.

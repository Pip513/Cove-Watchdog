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

**Six settings.** Everything else has a default in the code, so nothing else
needs to exist in Azure for the watchdog to work.

In the portal: **Function App → Settings → Environment variables → Add**

| Setting | |
|---|---|
| `COVE_PARTNER` | Console name **including** any parenthesised email |
| `COVE_USERNAME` | The API user's login name |
| `COVE_PASSWORD` | The token, shown once at creation |
| `SMTP_HOST` | Your mail host |
| `ALERT_FROM` | Sender your provider allows |
| `ALERT_TO` | Where alerts go; comma-separated |

That is the whole required setup. Nothing can automate it — a Cove token and
an SMTP host are values only you have.

Until all six have values, **every run fails loudly**. That is the design
refusing to look healthy while it is inert, not a broken deployment.

#### Changing a default

Anything in the [configuration reference](#configuration) can be overridden by
adding it as a setting. Common ones:

```
WATCHDOG_THRESHOLD_HOURS    4        hours before a data source counts as missed
WATCHDOG_MONITOR_PROFILE             profile names to watch; blank = all servers
WATCHDOG_IGNORE_PROFILE              profiles to mute
WATCHDOG_TIMEZONE           America/New_York
SMTP_PORT                   587
```

#### Optional: pre-create them all

To see the full tunable surface in the portal rather than having to know a
setting exists before you can change it:

```bash
pwsh scripts/setup-app-settings.ps1 -AppName "<app>" -ResourceGroup "<rg>"
pwsh scripts/setup-app-settings.ps1 -OutputJson     # no Azure CLI needed
```

It only adds what is missing, so a value you tuned is never reset.

`.github/workflows/configure-settings.yml` does the same on every push, but is
**off unless you set it up** — it needs `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`
and `AZURE_SUBSCRIPTION_ID` as secrets plus `AZURE_FUNCTIONAPP_NAME` and
`AZURE_RESOURCE_GROUP` as variables. Unconfigured it skips quietly. Worth
enabling only if several people will tune settings and you want them
discoverable; skip it otherwise.

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

At hourly execution this sits inside the Consumption plan's free grant. Expect
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

Full annotated list in [`.env.example`](.env.example). The ones you will
actually change:

| Setting | Default | |
|---|---|---|
| `WATCHDOG_THRESHOLD_HOURS` | `4` | Per data source. Set to roughly 4× your backup interval |
| `WATCHDOG_MONITOR_PROFILE` | *(blank)* | Comma-separated profile names. Blank = all servers |
| `WATCHDOG_IGNORE_PROFILE` | *(blank)* | Same format. Always wins. Move a device here to mute it |
| `WATCHDOG_TIMEZONE` | `America/New_York` | IANA name; handles DST |
| `WATCHDOG_REALERT_HOUR` | `8` | Local hour for the daily repeat |
| `REPORT_DAY` / `REPORT_HOUR` | `monday` / `8` | The heartbeat report |
| `ALERT_TO` | — | Comma-separated. Point at a ticket queue when ready |

---

## Development

```bash
.venv/bin/python test_detection.py    # detection rules, scoping, scheduling
.venv/bin/python test_failures.py     # failure classification and suppression
.venv/bin/python test_dispatch.py     # cadence, delivery, state
```

All three should pass before any change ships. They encode failure modes that
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

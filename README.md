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
- Python **3.11 or 3.12** (see the version note under Deploy)
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

### Python version

Azure Functions supports specific Python versions, and they lag the latest
release. Create the Function App with **3.11** (or 3.12 if your tooling offers
it) and develop against the same version locally. If your local Python is newer
than the Function App's runtime, the code will still run — nothing here uses
syntax past 3.10 — but you will not be testing what you deploy.

### Prerequisites

```bash
az --version          # Azure CLI
func --version        # Azure Functions Core Tools v4
az login
```

### 1. Create the resources

Names for the storage account and Function App must be **globally unique**. The
storage account must be lowercase alphanumeric, 3–24 characters.

```bash
RG=rg-cove-watchdog
LOCATION=eastus
STORAGE=covewatchdogsa$RANDOM
APP=cove-watchdog-$RANDOM

az group create --name $RG --location $LOCATION

az storage account create \
  --name $STORAGE \
  --resource-group $RG \
  --location $LOCATION \
  --sku Standard_LRS

az functionapp create \
  --name $APP \
  --resource-group $RG \
  --storage-account $STORAGE \
  --consumption-plan-location $LOCATION \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --os-type Linux

echo "Function App: $APP"
```

This creates an Application Insights resource automatically — you need it for
step 5.

> **Flex Consumption** is the newer plan and uses different flags and a
> different deployment mechanism. The commands above are for classic
> Consumption. If you want Flex, check the current Microsoft documentation
> rather than adapting these.

**No table needs creating.** State lives in Azure Table Storage in the account
above, reached through the `AzureWebJobsStorage` connection string the Function
App already has, and the table is created on first use.

### 2. Configure application settings

Every setting in `.env.example` is read from the environment, and Azure
application settings *are* environment variables — so the names are identical.

```bash
az functionapp config appsettings set \
  --name $APP --resource-group $RG \
  --settings \
    "COVE_PARTNER=Acme Ltd (admin@acme.com)" \
    "COVE_USERNAME=your-api-user-login-name" \
    "COVE_PASSWORD=the-token-from-the-console" \
    "WATCHDOG_MONITOR_PROFILE=1 hour RPO Server" \
    "WATCHDOG_TIMEZONE=America/New_York" \
    "SMTP_HOST=smtp.example.com" \
    "SMTP_PORT=587" \
    "SMTP_SECURITY=starttls" \
    "SMTP_USERNAME=your-smtp-user" \
    "SMTP_PASSWORD=your-smtp-password" \
    "ALERT_FROM=backups@example.com" \
    "ALERT_TO=you@example.com"
```

Everything else has a working default. See `.env.example` for the full list.

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

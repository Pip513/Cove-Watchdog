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

- An N-able Cove account, and permission to create an API user
- An SMTP host — a provider or an internal relay
- A GitHub account, to fork this repository
- An Azure subscription where you can create resources **and assign roles**:
  Owner on the resource group, or Contributor plus User Access Administrator.
  Connecting GitHub to Azure gives the deploy identity a role, which
  Contributor alone cannot do
- Nothing installed on your machine, unless you also want to
  [run it locally](#run-it-locally-optional)

---

## Before you start

### Create a Cove API user

In the Management Console: **Management → Users → API Users → Add API user**.

- Requires a SuperUser or Administrator to create.
- Role: the lowest that can read devices, customers **and backup profiles**. At
  time of writing that is `Operator`. `Supporter` is lower but cannot read
  profiles, which the monitor/ignore profile settings need.
- Do **not** tick Security Officer.
- **The token is shown exactly once.** Put it in your password manager
  straight away.

An API user carries the `NonInteractive` flag and cannot log into the console at
all, which is why it is preferred over a normal user with API access.

### Collect your details

| You need | From |
|---|---|
| Cove partner name | The console, exactly as shown — see below |
| Cove API username and token | The step above |
| SMTP host, port, username and password | Your mail provider or relay |
| A sender address | One your provider lets you send as |
| Where alerts go | One or more addresses, or a ticket system |

> **The one that catches everybody:** the partner name must match the console
> exactly, and the console name usually includes the contact email in
> parentheses — `Acme Ltd (admin@acme.com)`, not `Acme Ltd`. The bare company
> name is rejected with *"Unknown partner/username or bad password"*, which
> reads like a credential problem and is not.

---

## Deploy to Azure

Everything here happens in a browser, on GitHub and in the Azure portal. Allow
about half an hour. To script it instead, see
[Deploy from the command line](#deploy-from-the-command-line).

### Python version, and why it decides your hosting plan

**This targets Python 3.14 on a Flex Consumption plan.** That combination has
the longest support runway of any current option — 3.14 is supported until
April 2029 — which suits a monitoring tool you want to set up once and leave
alone.

Azure Functions supports 3.10 through 3.14, but **the classic Consumption plan
stops at 3.12**. Newer versions only come to Flex Consumption, which is where
Microsoft is steering Consumption apps. So the version you want decides the
plan:

| Want | Plan | End of support |
|---|---|---|
| **3.14** | **Flex Consumption** | April 2029 |
| 3.13 | Flex Consumption | October 2029 |
| 3.12 | Either | October 2028 |
| 3.11 | Either | October 2027 |
| 3.10 | Either | **October 2026** — avoid |

Nothing in this project uses syntax past 3.10, so any version in that range
works. It is verified on 3.14: the test suites pass with deprecation warnings
escalated to errors, and every dependency imports cleanly. The portal's
**Version** list shows what your region actually offers; pick 3.14, or the
highest there.

### 1. Fork the repository

On this repository's GitHub page, select **Fork** and create it under your
account or organisation.

Your fork is public, as forks of a public repository always are. That is fine:
nothing secret ever goes into it, because credentials live only in Azure. If it
must be private, import the repository as a new one instead of forking. You
lose the one-click **Sync fork** for updates.

### 2. Create the Function App

In the Azure portal: **Create a resource → Function App → Create**, choose
**Flex Consumption**, then **Select**. Work through the tabs, leaving anything
not listed here at its default:

| Tab | Setting | Choose |
|---|---|---|
| Basics | Resource group | A new one, so removing the watchdog later is a single deletion |
| Basics | Function App name | Globally unique. Letters, digits and hyphens |
| Basics | Region | Any listed. Only regions that support Flex Consumption appear |
| Basics | Runtime stack | **Python** |
| Basics | Version | **3.14**, or the highest listed |
| Basics | Instance size | The smallest, 512 MB, is plenty and stretches the free grant furthest |
| Storage | Storage account | Create new — the default |
| Networking | Enable public access | **On** — the default. Off blocks the deploy from GitHub |
| Monitoring | Enable Application Insights | **Yes**. The alerts in step 6 need it |
| Deployment | Continuous deployment | **Enable**. Sign in to GitHub, then pick your fork and the `main` branch |
| Deployment | Authentication | The **identity** option, not basic authentication |
| Authentication | Authentication type | **Leave on secrets** — see below |

> **Leave the Authentication tab on secrets**, even though Microsoft's own guide
> suggests managed identity there. The watchdog remembers what it has already
> sent in Table Storage, and reaches it through the `AzureWebJobsStorage`
> connection string. Switching that tab to managed identity removes the
> connection string, and the watchdog falls back to a local file that does not
> work in Azure. Identities can be added once it works: see
> [Optional hardening](#optional-hardening).

Select **Review + create**, then **Create**. It takes a few minutes. Creating
it also commits a workflow file into your fork and starts the first deploy,
**which is broken**. The next step fixes it.

### 3. Fix the deploy workflow

The workflow Azure generates for Python deploys "successfully", and then the
Function never loads: `ModuleNotFoundError: No module named 'requests'`. It
installs the dependencies onto the build machine instead of into the package,
and its zip command skips the hidden folder they belong in.

In your fork on GitHub, open `.github/workflows/`. Azure added a file there
named after your app. Edit it with the pencil icon and change two lines:

| Step | Generated | Change to |
|---|---|---|
| Install | `pip install -r requirements.txt` | `pip install -r requirements.txt --target=".python_packages/lib/site-packages"` |
| Zip | `zip release.zip ./* -r` | `zip -r -q release.zip . -x '.git/*' '.github/*'` |

Commit to `main`. That runs the workflow again, and this time it deploys an app
that loads.

[`.github/deploy-workflow.example.yml`](.github/deploy-workflow.example.yml) has
the whole corrected build job, plus two checks that fail the build if the
dependencies ever go missing again. To use it, replace the generated build job
with it, and keep the generated deploy job: its secret names are unique to your
app.

**Check it worked.** The run passes under your fork's **Actions** tab, and the
Function App's **Overview** page lists `backup_watchdog` under **Functions**. If
the function is missing, the dependencies did not ship: recheck both edits.

- If the **Actions** tab says workflows are disabled on this fork, enable them
  and re-run the latest workflow.
- Reconfiguring **Deployment Center** later rewrites this file and brings the
  bug back. Reapply the fix.
- This route ships the whole repository except `.git` and `.github`;
  `.funcignore` only applies to command-line deploys. Nothing in the repository
  is secret, so that is harmless.

### 4. Add the settings

A deploy ships code only. Settings live in the Function App, and these must
have values before anything runs:

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

Everything else has a default in the code, but add the full set anyway so every
setting is visible and adjustable in the portal.
[`azure-app-settings.txt`](azure-app-settings.txt) holds all of them, with
defaults filled in and the required ones blank:

1. **Function App → Settings → Environment variables → Advanced edit**.
2. Put the cursor just before the final `]` at the very end of the text.
3. Paste everything below the file's `COPY BELOW THIS LINE` marker. The block
   starts with a comma so it joins the list that is already there.
4. **OK**, then **Apply**.
5. Fill in the required values listed above, then **Apply** again.

> **Advanced edit replaces everything.** Keep `AzureWebJobsStorage`,
> `APPLICATIONINSIGHTS_CONNECTION_STRING` and
> `DEPLOYMENT_STORAGE_CONNECTION_STRING` — losing them breaks the app.

Every setting's format is in the [configuration reference](#configuration).
For the Cove token and SMTP password, a Key Vault reference is better than the
value itself; see [Optional hardening](#optional-hardening). It can wait until
the watchdog works.

> **Do not set `WEBSITE_TIME_ZONE`.** The timer runs in UTC on purpose; the
> application converts to local time itself using `WATCHDOG_TIMEZONE` to decide
> when the daily re-alert and weekly report fire. Two independent timezone
> interpretations disagree at daylight-saving boundaries and will send at the
> wrong hour twice a year.

> **Port 25 is blocked outbound** on most Azure compute. Use 587 with STARTTLS,
> or 2525.

### 5. Test it

A healthy fleet sends no email, so waiting proves little. Instead, briefly make
every backup look late. In **Environment variables**, change three settings and
**Apply**:

| Setting | Test value | Effect |
|---|---|---|
| `WATCHDOG_SCHEDULE` | `0 */5 * * * *` | Runs every 5 minutes instead of hourly |
| `WATCHDOG_THRESHOLD_HOURS` | `0.1` | Anything not backed up in the last 6 minutes alerts |
| `WATCHDOG_TABLE_NAME` | `covewatchdogtest` | Keeps the test out of the real state table |

Within about ten minutes an alert should arrive for each monitored device, or a
single summary if there are more than `WATCHDOG_MAX_EMAILS_PER_RUN`. Runs, and
any errors, show on the function's **Invocations** tab and in **Log stream**.

Then put all three back — `0 0 * * * *`, `4` and `covewatchdog` — and
**Apply**. You can delete the test table from the storage account's **Storage
browser → Tables**.

**Why the throwaway table.** The watchdog remembers what it has sent. Test
against the real table and, when the threshold goes back to 4, every device
"recovers" and sends an all-clear. Remembering also means an alert already sent
today is not repeated, so a second test needs a new table name.

**If nothing arrives,** the change may have landed just after a run started:
wait for the next one. Then look on the **Invocations** tab for an error.

### 6. Alert if the watchdog stops

**This is not optional.** Everything above tells you when *backups* fail.
Nothing in this repository can tell you when the *Function* stops running,
because at that point none of it is running.

First confirm the runs are visible. In your **Application Insights** resource,
open **Logs** and run:

```kusto
requests
| where name == "backup_watchdog"
| project timestamp, success
| order by timestamp desc
```

You should see a row per run. Both rules below are built on this query, so if
it returns nothing after the function has run, sort that out before relying on
them.

Then, in the same resource, **Alerts → Create → Alert rule**, with **Custom log
search** as the signal. Create two.

**1. Failed run.** The Function raises only when it could not tell anyone, so
this means "a problem nobody has heard about".

```kusto
requests
| where name == "backup_watchdog" and success == false
```

Measure **Table rows**, aggregation **Count**, alert when **greater than 0**,
look back **1 hour**, check every **1 hour**.

**2. No successful run in 3 hours.** The Function has stopped being invoked:
deleted, stopped, a broken deploy, a lapsed subscription.

```kusto
requests
| where name == "backup_watchdog" and success == true
```

Measure **Table rows**, aggregation **Count**, alert when **less than 1**, look
back **3 hours**, check every **1 hour**.

For both, add an action group that emails you — ideally at an address that is
**not** the alerts inbox, so one broken mailbox cannot hide both.

**Then test them.** An untested dead-man's switch is decorative.

- *No successful run:* **Stop** the Function App from its **Overview** page.
  After a little over three hours the alert should fire. **Start** it again.
- *Failed run:* repeat step 5's test with a new table name, such as
  `covewatchdogtest2`, and `SMTP_HOST` set to `smtp.invalid`. The watchdog then
  has alerts it cannot send, which is exactly the case this rule is for. Put
  everything back afterwards.

The weekly report is the third layer: if it stops arriving on Monday morning,
something is wrong regardless of what any alert says.

### 7. Updating

On your fork's GitHub page, select **Sync fork → Update branch**. That updates
`main`, which runs your deploy workflow; there is nothing to do in Azure. The
sync never touches your workflow file, because that file exists only in your
fork.

Settings added by an update have defaults in the code, so the app keeps working
without them. To make a new one visible in the portal, compare
`azure-app-settings.txt` with your **Environment variables** and add what is
missing.

### Cost

At hourly execution this sits well inside the Flex Consumption plan's monthly
free grant. Expect to pay for the storage account, which will be pennies a
month, and a small fixed monthly charge for each alert rule. Application
Insights has a free ingestion allowance that this will not approach.

### Removing it

**Resource groups →** yours **→ Delete resource group**. That removes the
Function App, its storage, Application Insights and the deploy identity
together. A Log Analytics workspace picked during creation may live in a
different resource group, so check for it. Then delete your fork if you no
longer want it.

### Optional hardening

Both of these can be done any time after the watchdog works.

#### Credentials in Key Vault

Keeps the Cove token and SMTP password out of the app settings, so anyone who
can read the settings still cannot read the secrets.

1. Create a **Key Vault**. The defaults are fine, including the Azure RBAC
   permission model.
2. On the vault, **Access control (IAM) → Add role assignment**: give yourself
   **Key Vault Secrets Officer**, without which you cannot add secrets.
3. **Secrets → Generate/Import**: add the Cove token and the SMTP password, for
   example as `cove-password` and `smtp-password`.
4. On the Function App, **Settings → Identity**: turn **System assigned** on.
5. On the vault, **Access control (IAM) → Add role assignment**: give the
   Function App's identity **Key Vault Secrets User**.
6. Set `COVE_PASSWORD` to
   `@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/cove-password/)`,
   and `SMTP_PASSWORD` likewise, then **Apply**.

**Environment variables** shows whether each reference resolved. One that did
not passes the literal `@Microsoft.KeyVault(...)` text to the app, and the next
run fails and says so. No code change is needed: the application reads
everything through the environment.

#### State through a managed identity

By default state is reached with the storage connection string the Function
App already has. To use an identity instead:

1. On the Function App, **Settings → Identity**: turn **System assigned** on,
   if it is not already.
2. On the storage account, **Access control (IAM) → Add role assignment**: give
   the Function App's identity **Storage Table Data Contributor**.
3. Set `WATCHDOG_TABLE_ACCOUNT_URL` to
   `https://<storage-account>.table.core.windows.net`, then **Apply**.

The code prefers the identity whenever that URL is set, and stops using the
connection string for state.

### Deploy from the command line

<details>
<summary>Azure CLI and Core Tools instead of the portal</summary>

```bash
az --version          # Azure CLI, 2.60.0 or later for Flex Consumption
func --version        # Azure Functions Core Tools v4
az login
```

Ask your own subscription which Python versions and regions Flex offers, rather
than trusting any documentation, including this:

```bash
az functionapp list-runtimes --os linux \
  --query "linux[?starts_with(runtime,'python')].{runtime:runtime, version:version}" -o table

az functionapp list-flexconsumption-locations \
  --query "sort_by(@, &name)[].{Region:name}" -o table
```

Create the resources. Storage account names must be globally unique, lowercase
alphanumeric, 3–24 characters:

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

This creates Application Insights too. No table needs creating: it is created
on first use.

Classic Consumption instead, where Flex is not offered — capped at Python 3.12,
with a documented migration path away from it:

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

Add every setting. This adds only what is missing and never overwrites, so it
is safe to re-run after an update introduces a new setting. Then fill in the
required values as in step 4:

```bash
pwsh scripts/setup-app-settings.ps1 -AppName $APP -ResourceGroup $RG
```

`pwsh scripts/setup-app-settings.ps1 -OutputJson` prints the paste block from
step 4 instead.

Deploy. This builds on Azure and honours `.funcignore`, which keeps tests,
exploration scripts, `.env` and local state out of the package:

```bash
func azure functionapp publish $APP
```

Test, as in step 5, and watch the output:

```bash
az functionapp config appsettings set --name $APP --resource-group $RG \
  --settings "WATCHDOG_SCHEDULE=0 */5 * * * *" "WATCHDOG_THRESHOLD_HOURS=0.1" \
             "WATCHDOG_TABLE_NAME=covewatchdogtest"

az webapp log tail --name $APP --resource-group $RG

az functionapp config appsettings set --name $APP --resource-group $RG \
  --settings "WATCHDOG_SCHEDULE=0 0 * * * *" "WATCHDOG_THRESHOLD_HOURS=4" \
             "WATCHDOG_TABLE_NAME=covewatchdog"
```

Step 6 still applies. Remove everything with:

```bash
az group delete --name $RG --yes
```

</details>

---

## Run it locally (optional)

Useful before deploying, to see exactly what the watchdog would do against your
fleet, and later for debugging. Nothing here writes to Cove, and nothing sends
email unless you pass `--send`.

Use the same Python version you deploy. If they differ you are not testing what
you ship.

### 1. Configure

```bash
cp .env.example .env
```

Fill it in with the details from [Before you start](#before-you-start).

### 2. Install and verify

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

### 3. Test email delivery

Fill in the SMTP block in `.env`, then:

```bash
.venv/bin/python send_test_email.py --send
```

That renders a realistic alert from a **synthetic** device, so you can prove
delivery without waiting for a real failure or mailing about a healthy machine.

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

#### How `WATCHDOG_SCHEDULE` works

It controls how often the watchdog **checks** — not when emails go out. Email
timing uses local time, from `WATCHDOG_REALERT_HOUR` and
`REPORT_DAY`/`REPORT_HOUR`.

The format is Azure's NCRONTAB: six fields separated by spaces, from the
smallest unit to the largest.

```
0   0   *   *   *   *
│   │   │   │   │   └── day of week   0-6 (0 = Sunday), or Sun-Sat
│   │   │   │   └────── month         1-12, or Jan-Dec
│   │   │   └────────── day of month  1-31
│   │   └────────────── hour          0-23, in UTC
│   └────────────────── minute        0-59
└────────────────────── second        0-59
```

So `0 0 * * * *` reads as "second 0, minute 0, any hour, any day": the top of
every hour.

| Symbol | Meaning | Example | Result |
|---|---|---|---|
| `*` | every value | `*` in hour | every hour |
| a number | exactly that value | `30` in minute | at :30 |
| `,` | a list | `0,30` in minute | at :00 and :30 |
| `-` | a range | `9-17` in hour | 9 am through 5 pm (UTC) |
| `*/n` | every *n*th | `*/15` in minute | at :00, :15, :30, :45 |

Useful values:

| Schedule | Runs | Use |
|---|---|---|
| `0 0 * * * *` | hourly, on the hour | **Recommended** |
| `0 */15 * * * *` | every 15 minutes | Faster detection; the extra cost is negligible |
| `0 30 * * * *` | hourly, at half past | Same as hourly, offset |
| `0 */5 * * * *` | every 5 minutes | Testing only |

**Three traps:**

1. **It is UTC.** `0 0 9 * * *` is 9:00 UTC, which is 5 am EDT or 4 am EST.
   That is deliberate — the app converts to local time itself — so do not set
   `WEBSITE_TIME_ZONE` to "fix" it.
2. **Always put `0` in the seconds field.** A `*` there means *every second*:
   `* * * * * *` would call the Cove API 86,400 times a day.
3. **Count the fields.** Azure also accepts standard five-field cron, which has
   no seconds field, and decides which you meant by counting. Similar-looking
   strings therefore differ wildly: `0 */5 * * * *` (six fields) runs every
   five *minutes*, while `0 */5 * * *` (five fields) runs every five *hours*.

**Do not go slower than hourly.** The daily reminder and the weekly report each
go out on the first run *at or after* their hour. On a two-hourly schedule they
can arrive up to two hours late, and missed backups are noticed up to two hours
later too. Faster than hourly is fine and costs almost nothing.

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

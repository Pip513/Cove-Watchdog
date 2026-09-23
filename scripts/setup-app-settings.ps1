# Cove Watchdog - Azure Function App settings setup
#
# Creates every application setting the watchdog reads, so the whole
# configuration surface is visible in the portal rather than discovered one
# failure at a time.
#
# ONLY ADDS SETTINGS THAT DO NOT ALREADY EXIST. Never overwrites. Safe to run
# on every deploy: a threshold you tuned in the portal will not be reset.
#
# Secrets are created BLANK on purpose. Fill them in the Azure portal, or point
# them at Key Vault. They are deliberately not passed in here, so they never
# pass through CI logs or a shell history.
#
# Usage:
#   ./setup-app-settings.ps1 -AppName "cove-watchdog" -ResourceGroup "Cove-Watchdog"
#   ./setup-app-settings.ps1 -WhatIf          # show what would change
#
# Requires: Azure CLI, logged in with rights to the Function App.

param(
    [string]$AppName       = $env:AZURE_FUNCTIONAPP_APP_NAME,
    [string]$ResourceGroup = $env:AZURE_RESOURCE_GROUP,
    [switch]$WhatIf,
    # Print the settings as portal "Advanced edit" JSON and exit. Needs no
    # Azure CLI and no login - useful when az is not installed.
    [switch]$OutputJson
)

$ErrorActionPreference = "Stop"

if (-not $OutputJson -and -not $AppName) {
    Write-Error "AppName is required. Pass -AppName or set AZURE_FUNCTIONAPP_APP_NAME."
    exit 1
}
if (-not $OutputJson -and -not $ResourceGroup) {
    Write-Error "ResourceGroup is required. Pass -ResourceGroup or set AZURE_RESOURCE_GROUP."
    exit 1
}

# Settings the watchdog cannot run without. Reported at the end if still blank.
$required = @(
    "COVE_PARTNER", "COVE_USERNAME", "COVE_PASSWORD",
    "SMTP_HOST", "ALERT_FROM", "ALERT_TO"
)

# Key Vault reference format, for the secrets:
#   @Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/<name>/)

$settings = [ordered]@{

    # --- Cove API ---------------------------------------------------------
    # Create a dedicated API user: Management > Users > API Users.
    # Role Operator (lowest that can read backup profiles). No Security Officer.

    # Customer name EXACTLY as the console shows it. This usually INCLUDES the
    # contact email in parentheses, e.g. "Acme Ltd (admin@acme.com)". The bare
    # company name is rejected as a bad password, which is misleading.
    "COVE_PARTNER"                = ""
    "COVE_USERNAME"               = ""   # the API user's "Requested login name"
    "COVE_PASSWORD"               = ""   # token, shown once at creation. KV ref here
    "COVE_ENDPOINT"               = "https://api.backup.management/jsonapi"

    # --- Detection --------------------------------------------------------
    # Hours without a successful backup before a data source counts as missed.
    # Each active data source is checked independently. Roughly 4x the backup
    # interval is a sane starting point.
    "WATCHDOG_THRESHOLD_HOURS"    = "4"

    # Hours after a device is created during which it cannot alert. Covers the
    # initial seed, which legitimately runs long with no prior success.
    "WATCHDOG_GRACE_HOURS"        = "24"

    # Backup profile names to monitor, EXACTLY as the console shows them.
    # Comma-separated. Blank = monitor every server instead.
    # Matching is exact, not substring: "1 hour RPO" would otherwise also catch
    # "1 hour RPO Server". A name matching no device is a hard failure, so a
    # profile renamed in the console cannot silently leave you watching nothing.
    "WATCHDOG_MONITOR_PROFILE"    = ""

    # Profiles never alerted on. Same format. Always wins over the above, so
    # moving a device to this profile in the console mutes it.
    "WATCHDOG_IGNORE_PROFILE"     = ""

    # IANA timezone for email timestamps, the daily re-alert and the weekly
    # report. Handles daylight saving. Do NOT also set WEBSITE_TIME_ZONE - the
    # app converts from UTC itself, and two interpretations disagree at DST.
    "WATCHDOG_TIMEZONE"           = "America/New_York"

    # Local hour for the daily repeat on a device still failing. The first
    # alert always fires immediately, whatever the hour.
    "WATCHDOG_REALERT_HOUR"       = "8"

    # Above this many device alerts in one run, a single summary is sent
    # instead. Guards against a site-wide outage opening a hundred tickets.
    "WATCHDOG_MAX_EMAILS_PER_RUN" = "25"

    # --- Weekly report ----------------------------------------------------
    # Sent whether or not anything is wrong. Its absence is the signal that the
    # watchdog itself has stopped, so leaving this enabled is recommended.
    "REPORT_ENABLED"              = "true"
    "REPORT_DAY"                  = "monday"
    "REPORT_HOUR"                 = "8"
    "REPORT_DEVICE_LIMIT"         = "5"    # devices listed before collapsing to a count

    # --- Email (SMTP) -----------------------------------------------------
    # Any SMTP sender: a hosted provider or an internal relay.
    "SMTP_HOST"                   = ""
    # 587 STARTTLS, 465 implicit SSL, 2525 common alternate.
    # NOT 25 - blocked outbound on most Azure compute.
    "SMTP_PORT"                   = "587"
    "SMTP_SECURITY"               = "starttls"   # starttls | ssl | none
    "SMTP_USERNAME"               = ""    # blank for an unauthenticated relay
    "SMTP_PASSWORD"               = ""    # KV ref here
    "SMTP_VERIFY_CERT"            = "false" # true to verify the server certificate; needs a valid one
    "SMTP_TIMEOUT"                = "30"

    # --- Alert addressing -------------------------------------------------
    "ALERT_FROM"                  = ""    # must be an address your provider allows
    "ALERT_FROM_NAME"             = "Cove Backup Watchdog"
    "ALERT_TO"                    = ""    # comma-separated; a ticket queue when ready
    "ALERT_SUBJECT_PREFIX"        = "[Cove]"

    # --- State ------------------------------------------------------------
    # State lives in Azure Table Storage. With neither of the two settings
    # below, it uses the AzureWebJobsStorage connection the Function App
    # already has, and creates the table on first use. Nothing to do.
    "WATCHDOG_TABLE_NAME"         = "covewatchdog"

    # Optional hardening: set to https://<storage>.table.core.windows.net to
    # use a managed identity instead of a connection string. Needs a
    # system-assigned identity with Storage Table Data Contributor.
    "WATCHDOG_TABLE_ACCOUNT_URL"  = ""

    # Optional: a different storage account for state. Blank uses
    # AzureWebJobsStorage.
    "WATCHDOG_STATE_CONNECTION"   = ""

    # --- Schedule ---------------------------------------------------------
    # Azure NCRONTAB, five or six fields; six puts seconds first. This is the
    # six-field form. Count carefully: "0 */5 * * * *" is every five minutes,
    # "0 */5 * * *" every five hours. This value is hourly, on the
    # hour, in UTC. Temporarily set "0 */5 * * * *" to test a deployment.
    "WATCHDOG_SCHEDULE"           = "0 0 * * * *"
}

# --- JSON output, for the portal when Azure CLI is not available ---------
if ($OutputJson) {
    $payload = foreach ($key in $settings.Keys) {
        [ordered]@{ name = $key; value = $settings[$key]; slotSetting = $false }
    }
    # Guidance goes to stderr so stdout is pure JSON and
    #   ./setup-app-settings.ps1 -OutputJson > settings.json
    # produces a file you can paste straight in.
    $err = [Console]::Error
    $err.WriteLine("")
    $err.WriteLine("Function App > Settings > Environment variables > Advanced edit")
    $err.WriteLine("")
    $err.WriteLine("WARNING: Advanced edit REPLACES every setting with what you paste.")
    $err.WriteLine("Copy the existing JSON out first and keep these three, or the app breaks:")
    $err.WriteLine("  AzureWebJobsStorage")
    $err.WriteLine("  APPLICATIONINSIGHTS_CONNECTION_STRING")
    $err.WriteLine("  DEPLOYMENT_STORAGE_CONNECTION_STRING")
    $err.WriteLine("")
    $err.WriteLine("Paste the block below INSIDE the existing array, alongside those.")
    $err.WriteLine("")
    # Trim the outer brackets so it drops into an existing array cleanly.
    $json = $payload | ConvertTo-Json -Depth 3
    $inner = ($json -split "`n" | Select-Object -Skip 1 | Select-Object -SkipLast 1) -join "`n"
    Write-Output ("," + $inner)
    exit 0
}

Write-Host "Function App : $AppName"
Write-Host "Resource group: $ResourceGroup"
Write-Host ""
Write-Host "Reading current settings..."

$existing = az functionapp config appsettings list `
    --name $AppName `
    --resource-group $ResourceGroup 2>$null | ConvertFrom-Json

if (-not $existing) {
    Write-Error @"
Could not read settings. Check that:
  - you are logged in (az login)
  - the app name and resource group are correct
  - your account has rights to the Function App
"@
    exit 1
}

$existingByName = @{}
foreach ($item in $existing) { $existingByName[$item.name] = $item.value }

$toSet = @()
foreach ($key in $settings.Keys) {
    if ($existingByName.ContainsKey($key)) {
        Write-Host "  skip  $key" -ForegroundColor DarkGray
    } else {
        $toSet += "$key=$($settings[$key])"
        Write-Host "  add   $key" -ForegroundColor Green
    }
}

Write-Host ""

if ($toSet.Count -eq 0) {
    Write-Host "All settings already present. Nothing added."
} elseif ($WhatIf) {
    Write-Host "$($toSet.Count) setting(s) would be added. Re-run without -WhatIf to apply."
} else {
    Write-Host "Adding $($toSet.Count) setting(s)..."
    az functionapp config appsettings set `
        --name $AppName `
        --resource-group $ResourceGroup `
        --settings @toSet | Out-Null
    Write-Host "Done."

    # Re-read so the check below reflects what is actually in Azure.
    $existing = az functionapp config appsettings list `
        --name $AppName --resource-group $ResourceGroup 2>$null | ConvertFrom-Json
    $existingByName = @{}
    foreach ($item in $existing) { $existingByName[$item.name] = $item.value }
}

# --- What still needs a human -------------------------------------------
$blank = @()
foreach ($key in $required) {
    $value = $existingByName[$key]
    if ([string]::IsNullOrWhiteSpace($value)) { $blank += $key }
}

Write-Host ""
if ($blank.Count -gt 0) {
    Write-Host "NOT YET CONFIGURED - the watchdog cannot run until these have values:" -ForegroundColor Yellow
    foreach ($key in $blank) { Write-Host "  $key" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "Set them in the portal under $AppName > Settings > Environment variables,"
    Write-Host "or point the two secrets at Key Vault. Until then every run will fail"
    Write-Host "loudly, which is by design - it will not quietly look healthy."
    exit 2
}

Write-Host "All required settings have values." -ForegroundColor Green
Write-Host "Check a run under $AppName > Functions > backup_watchdog > Monitor."

<#
.SYNOPSIS
    Run one Monitra scheduled job by calling its internal endpoint on this server.

.DESCRIPTION
    The Windows counterpart of deploy/backend/scheduled-jobs/monitra-job.sh.
    Task Scheduler runs this once per firing; the endpoint does the work, this only
    knocks. It talks to the backend directly on the loopback address, never through
    IIS or the public name: IIS refuses a body-less POST with HTTP 411, and a proxy
    timeout would cut a long job short.

    EMAIL_DISPATCH_TOKEN is taken from the process environment, then the machine
    environment, then <BackendPath>\.env, and is sent only as the
    X-Email-Dispatch-Token header. It never appears in an argument, a URL or the log.

    Exit codes (what Task Scheduler shows as "Last Run Result"):
        0  the endpoint answered 2xx
        2  unknown job name
        3  no EMAIL_DISPATCH_TOKEN could be found
        4  the backend could not be reached, or did not answer in time
        5  the backend answered with a non-2xx status (401 = wrong token)

.PARAMETER Job
    One of: email-dispatch, budget-alerts, weekly-report, monthly-report,
    monthly-project-summary, daily-rollup, wfpm-timer-dispatch.

.PARAMETER Attempts
    How many times to try when the backend cannot be reached or answers 5xx (a restart in
    progress, a transient fault). Default 1. The rare jobs are installed with 3, so one
    unlucky minute at 09:00 on the 1st does not cost a whole month's report.

.PARAMETER RetryDelaySec
    Seconds to wait between attempts. Default 300.

.PARAMETER Check
    Do not run the job. Report where the token was found and whether the backend
    answers /health, then exit 0 only if both are fine. Sends nothing.

.EXAMPLE
    .\Monitra-Job.ps1 -Job email-dispatch -BackendPath D:\monitra\backend
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Job,
    [string]$BackendPath = '',
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [int]$TimeoutSec = 840,
    [int]$Attempts = 1,
    [int]$RetryDelaySec = 300,
    [string]$LogPath = '',
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# job name -> endpoint. tests/test_scheduled_jobs_windows.py keeps this in step with
# vercel.json and with the Linux runner's case statement.
$Endpoints = @{
    'email-dispatch'          = '/internal/email/dispatch'
    'budget-alerts'           = '/internal/project-budget-alerts/run'
    'weekly-report'           = '/internal/reports/weekly/run'
    'monthly-report'          = '/internal/reports/monthly/run'
    'monthly-project-summary' = '/internal/reports/monthly-projects/run'
    'daily-rollup'            = '/internal/activity/daily-rollup'
    'wfpm-timer-dispatch'     = '/internal/wfpm/timer-events/dispatch'
}

if (-not $LogPath) { $LogPath = Join-Path $env:ProgramData 'Monitra\logs\monitra-jobs.log' }

function Protect-Text([string]$text) {
    if ([string]::IsNullOrEmpty($text)) { return '' }
    $text = [regex]::Replace($text, '(?i)((?:pass(?:word|wd)?|secret|token|api[_-]?key|authorization|bearer)["'']?\s*[=:]\s*["'']?)[^\s"'',}]+', '$1[redacted]')
    return ($text -replace '[\r\n]+', ' ')
}

# Logging must never fail a job, and two jobs can fire in the same second.
function Write-JobLog([string]$text) {
    $line = '{0} job={1} {2}' -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $Job, (Protect-Text $text)
    for ($try = 0; $try -lt 4; $try++) {
        try {
            $dir = Split-Path -Parent $LogPath
            if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
            if ((Test-Path -LiteralPath $LogPath) -and ((Get-Item -LiteralPath $LogPath).Length -gt 5MB)) {
                Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.1') -Force
            }
            Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
            break
        } catch { Start-Sleep -Milliseconds 250 }
    }
    Write-Output $line
}

# Same value dotenv gives the backend: unquoted values lose a trailing " # comment".
function Read-DotEnvValue([string]$path, [string]$name) {
    foreach ($raw in Get-Content -LiteralPath $path) {
        if ($raw -match ('^\s*(?:export\s+)?' + [regex]::Escape($name) + '\s*=\s*(.*)$')) {
            $v = $Matches[1].Trim()
            if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[$v.Length - 1] -eq '"') -or ($v[0] -eq "'" -and $v[$v.Length - 1] -eq "'"))) {
                return $v.Substring(1, $v.Length - 2)
            }
            return (($v -replace '\s+#.*$', '')).Trim()
        }
    }
    return ''
}

# Returns @{ Token = ...; Source = ... }. Same precedence the backend uses: a real
# environment variable beats the .env file.
function Find-Token {
    $t = $env:EMAIL_DISPATCH_TOKEN
    if ($t -and $t.Trim()) { return @{ Token = $t.Trim(); Source = 'process environment' } }
    $t = [Environment]::GetEnvironmentVariable('EMAIL_DISPATCH_TOKEN', 'Machine')
    if ($t -and $t.Trim()) { return @{ Token = $t.Trim(); Source = 'machine environment' } }
    if ($BackendPath) {
        $envFile = Join-Path $BackendPath '.env'
        if (Test-Path -LiteralPath $envFile) {
            try {
                $t = Read-DotEnvValue $envFile 'EMAIL_DISPATCH_TOKEN'
                if ($t) { return @{ Token = $t; Source = $envFile } }
            } catch { }
        }
    }
    return @{ Token = ''; Source = '' }
}

if (-not $Endpoints.ContainsKey($Job)) {
    Write-JobLog ("unknown job; expected one of: " + (($Endpoints.Keys | Sort-Object) -join ', '))
    exit 2
}

$found = Find-Token
$base = $BaseUrl.TrimEnd('/')

if ($Check) {
    Write-Output ("job        : $Job -> " + $Endpoints[$Job])
    Write-Output ("backend    : $base")
    if ($found.Token) { Write-Output ("token      : found in " + $found.Source) } else { Write-Output 'token      : NOT FOUND (process env, machine env, <BackendPath>\.env)' }
    $healthy = $false
    try {
        $h = Invoke-WebRequest -Uri ($base + '/health') -UseBasicParsing -TimeoutSec 20
        Write-Output ("health     : HTTP " + [int]$h.StatusCode)
        $healthy = ([int]$h.StatusCode -eq 200)
    } catch { Write-Output ("health     : unreachable (" + (Protect-Text $_.Exception.Message) + ')') }
    if ($found.Token -and $healthy) { exit 0 }
    exit 1
}

if (-not $found.Token) {
    Write-JobLog 'EMAIL_DISPATCH_TOKEN not found (process env, machine env, <BackendPath>\.env)'
    exit 3
}

$uri = $base + $Endpoints[$Job]
$maxAttempts = [Math]::Max(1, $Attempts)
$exitCode = 5

for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $status = 0
    $body = ''
    $unreachable = ''
    try {
        # An empty body, so the request carries Content-Length: 0.
        $r = Invoke-WebRequest -Uri $uri -Method Post -Headers @{ 'X-Email-Dispatch-Token' = $found.Token } `
            -Body '' -ContentType 'application/json' -UseBasicParsing -TimeoutSec $TimeoutSec
        $status = [int]$r.StatusCode
        $body = [string]$r.Content
    } catch {
        $ex = $_.Exception
        if ($ex.Response) {
            $status = [int]$ex.Response.StatusCode
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $body = $_.ErrorDetails.Message
            } else {
                try { $body = (New-Object System.IO.StreamReader($ex.Response.GetResponseStream())).ReadToEnd() } catch { $body = '' }
            }
        } else {
            $unreachable = $ex.Message
        }
    }
    $clock.Stop()

    if ($unreachable) {
        Write-JobLog ("could not reach $uri after {0} ms: {1}" -f $clock.ElapsedMilliseconds, $unreachable)
        $exitCode = 4
    } else {
        if ($body.Length -gt 600) { $body = $body.Substring(0, 600) }
        Write-JobLog ("-> HTTP {0} in {1} ms {2}" -f $status, $clock.ElapsedMilliseconds, $body)
        if ($status -ge 200 -and $status -lt 300) {
            $exitCode = 0
        } else {
            $exitCode = 5
            if ($status -eq 401) {
                Write-JobLog ("HTTP 401 - the dispatch secret this job read (from {0}) differs from the one the backend is running with" -f $found.Source)
            }
        }
    }

    # A backend that is restarting, or a server error, is worth another try; a refused token
    # or a 4xx is not. Every job is idempotent, so a repeated call can never send twice.
    $transient = ($exitCode -eq 4) -or ($status -ge 500)
    if ($exitCode -eq 0 -or -not $transient -or $attempt -ge $maxAttempts) { break }
    Write-JobLog ("attempt {0} of {1} failed; trying again in {2} s" -f $attempt, $maxAttempts, $RetryDelaySec)
    Start-Sleep -Seconds $RetryDelaySec
}
exit $exitCode

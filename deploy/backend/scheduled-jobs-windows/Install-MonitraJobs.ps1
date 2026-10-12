<#
.SYNOPSIS
    Install, check or remove Monitra's scheduled jobs on this Windows server.

.DESCRIPTION
    The Windows counterpart of deploy/backend/scheduled-jobs/install.sh. It registers
    one Task Scheduler task per job under the \Monitra folder. Each task runs
    Monitra-Job.ps1, which calls the job's internal endpoint on 127.0.0.1.

    The schedule is the cron string in vercel.json (UTC), exactly as the Linux timers
    use it, written with a trailing Z so Task Scheduler fires at the same instant
    whatever time zone the server is set to.
      * Daily, weekly and monthly jobs catch up when the server was off at their time
        (the Windows form of systemd's Persistent=true) and make up to three attempts,
        five minutes apart, if the backend is not answering (it may be restarting).
        Every job is idempotent, so a late or repeated run never sends twice.
      * The every-minute and every-five-minute jobs do not catch up or retry: the next
        firing does the same work.
      * A job never overlaps itself. The frequent jobs are stopped after 15 minutes, the
        rare ones (which may be retrying) after an hour.

    Safe to run again at any time; it replaces the tasks it owns.

.PARAMETER BackendPath
    The backend folder (the one holding .env). The token is read from there unless it
    is already set in the machine environment.

.PARAMETER Port
    The port uvicorn listens on, on this server. Default 8000.

.PARAMETER RunAsUser
    SYSTEM (default), or a Windows account such as DOMAIN\svc-monitra. A named account
    is asked for its password once (Get-Credential); the password is handed to Task
    Scheduler and kept nowhere else. It must be allowed to "Log on as a batch job" and
    must be able to read <BackendPath>\.env.

.PARAMETER RunOnlyWhenLoggedOn
    Testing only: run as the current user, only while that user is logged on. No
    elevation or password needed.

.PARAMETER Status
    Show each task's state, last result and next run, and the last log lines. Changes nothing.

.PARAMETER RunNow
    After installing, fire email-dispatch once and show how it went.

.PARAMETER Uninstall
    Remove the tasks. The runner copy and the log are left in place.

.PARAMETER ShowXml
    Print each task's Task Scheduler definition and register nothing.

.EXAMPLE
    .\Install-MonitraJobs.ps1 -BackendPath D:\monitra\backend -Port 8000 -RunNow

.EXAMPLE
    .\Install-MonitraJobs.ps1 -Status
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$BackendPath = '',
    [int]$Port = 8000,
    [string]$RunAsUser = 'SYSTEM',
    [string]$TaskFolder = '\Monitra',
    [string]$InstallDir = (Join-Path $env:ProgramData 'Monitra\bin'),
    [string]$LogPath = (Join-Path $env:ProgramData 'Monitra\logs\monitra-jobs.log'),
    [switch]$RunOnlyWhenLoggedOn,
    [switch]$Status,
    [switch]$RunNow,
    [switch]$Uninstall,
    [switch]$ShowXml,
    [switch]$SkipCheck
)

$ErrorActionPreference = 'Stop'

# The same cron strings as vercel.json, in UTC. tests/test_scheduled_jobs_windows.py keeps
# this table identical to the crons there and to the endpoints in Monitra-Job.ps1.
$Jobs = @(
    @{ Name = 'email-dispatch';          Cron = '*/5 * * * *' },
    @{ Name = 'budget-alerts';           Cron = '*/5 * * * *' },
    @{ Name = 'weekly-report';           Cron = '30 3 * * 1' },
    @{ Name = 'monthly-report';          Cron = '30 3 1 * *' },
    @{ Name = 'monthly-project-summary'; Cron = '30 3 1 * *' },
    @{ Name = 'daily-rollup';            Cron = '50 18 * * *' },
    @{ Name = 'wfpm-timer-dispatch';     Cron = '*/1 * * * *' },
    @{ Name = 'timer-reconcile';         Cron = '*/5 * * * *' }
)

$DayNames = 'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'
$MonthNames = 'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'
$folder = '\' + $TaskFolder.Trim('\') + '\'
$RunnerSource = Join-Path $PSScriptRoot 'Monitra-Job.ps1'
$RunnerPath = Join-Path $InstallDir 'Monitra-Job.ps1'
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-TaskName([string]$job) { return "monitra-job-$job" }

# cron (UTC) -> a Task Scheduler trigger. Only the shapes vercel.json uses are accepted;
# anything else is an error here rather than a job that silently never fires.
function ConvertTo-Trigger([string]$Cron) {
    $f = $Cron.Trim() -split '\s+'
    if ($f.Count -ne 5 -or $f[3] -ne '*') { throw "Unsupported cron expression: '$Cron'" }
    $minute = $f[0]; $hour = $f[1]; $dom = $f[2]; $dow = $f[4]

    if ($minute -match '^\*/(\d+)$' -and $hour -eq '*' -and $dom -eq '*' -and $dow -eq '*') {
        $every = [int]$Matches[1]
        # Anchored on a UTC midnight, so the firings land on :00, :05, :10 ... UTC.
        return @{
            Frequent = $true
            Xml = "<TimeTrigger><Repetition><Interval>PT${every}M</Interval></Repetition><StartBoundary>2026-01-01T00:00:00Z</StartBoundary><Enabled>true</Enabled></TimeTrigger>"
        }
    }
    if ($minute -notmatch '^\d{1,2}$' -or $hour -notmatch '^\d{1,2}$') { throw "Unsupported cron expression: '$Cron'" }
    $at = '2026-01-01T{0:00}:{1:00}:00Z' -f [int]$hour, [int]$minute
    $head = "<CalendarTrigger><StartBoundary>$at</StartBoundary><Enabled>true</Enabled>"

    if ($dom -eq '*' -and $dow -eq '*') {
        return @{ Frequent = $false; Xml = "$head<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>" }
    }
    if ($dom -eq '*' -and $dow -match '^[0-6]$') {
        $day = $DayNames[[int]$dow]
        return @{ Frequent = $false; Xml = "$head<ScheduleByWeek><DaysOfWeek><$day /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>" }
    }
    if ($dow -eq '*' -and $dom -match '^\d{1,2}$') {
        $months = ($MonthNames | ForEach-Object { "<$_ />" }) -join ''
        return @{ Frequent = $false; Xml = "$head<ScheduleByMonth><DaysOfMonth><Day>$([int]$dom)</Day></DaysOfMonth><Months>$months</Months></ScheduleByMonth></CalendarTrigger>" }
    }
    throw "Unsupported cron expression: '$Cron'"
}

function New-TaskXml([hashtable]$Job, [string]$Mode, [string]$UserId) {
    $trigger = ConvertTo-Trigger $Job.Cron
    # Catch up after downtime, and try again if the backend is restarting or faulting -- only
    # for the jobs that fire rarely: a missed monthly report would otherwise wait a month.
    # The frequent jobs do the same work again at their next firing. (Task Scheduler's own
    # "restart on failure" was tried and does not act on a non-zero exit code, so the retry
    # lives in the runner.)
    if ($trigger.Frequent) {
        $catchUp = '<StartWhenAvailable>false</StartWhenAvailable>'
        $attemptArgs = ''
        $timeLimit = 'PT15M'
    } else {
        $catchUp = '<StartWhenAvailable>true</StartWhenAvailable>'
        $attemptArgs = ' -Attempts 3 -RetryDelaySec 300'
        $timeLimit = 'PT1H'
    }
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Job {1} -BackendPath "{2}" -BaseUrl "http://127.0.0.1:{3}" -LogPath "{4}"{5}' -f `
        $RunnerPath, $Job.Name, $BackendPath, $Port, $LogPath, $attemptArgs
    $arguments = [Security.SecurityElement]::Escape($arguments)
    $description = [Security.SecurityElement]::Escape("Monitra scheduled job $($Job.Name) (cron '$($Job.Cron)', UTC). Installed by Install-MonitraJobs.ps1.")

    switch ($Mode) {
        'System'      { $principal = '<UserId>S-1-5-18</UserId><RunLevel>LeastPrivilege</RunLevel>' }
        'Interactive' { $principal = '<UserId>' + [Security.SecurityElement]::Escape($UserId) + '</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>' }
        default       { $principal = '<RunLevel>LeastPrivilege</RunLevel>' }   # the account is applied at registration
    }
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$description</Description></RegistrationInfo>
  <Triggers>$($trigger.Xml)</Triggers>
  <Principals><Principal id="Author">$principal</Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    $catchUp
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>$timeLimit</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>$PowerShellExe</Command><Arguments>$arguments</Arguments></Exec></Actions>
</Task>
"@
}

function Get-ResultText([int64]$code) {
    switch ($code) {
        0        { return 'OK' }
        267009   { return 'running' }
        267011   { return 'not run yet' }
        1        { return 'failed (check the log)' }
        2        { return 'unknown job name' }
        3        { return 'no EMAIL_DISPATCH_TOKEN found' }
        4        { return 'backend unreachable' }
        5        { return 'backend answered non-2xx (see log)' }
        default  { return ('0x{0:X}' -f $code) }
    }
}

function Show-Status {
    Write-Host "Monitra scheduled jobs in $folder" -ForegroundColor Cyan
    $rows = foreach ($j in $Jobs) {
        $t = Get-ScheduledTask -TaskPath $folder -TaskName (Get-TaskName $j.Name) -ErrorAction SilentlyContinue
        if (-not $t) {
            [pscustomobject]@{ Job = $j.Name; State = 'NOT INSTALLED'; LastRun = ''; Result = ''; NextRun = ''; Missed = '' }
            continue
        }
        $i = $t | Get-ScheduledTaskInfo
        $last = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 1999) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { 'never' }
        $next = if ($i.NextRunTime -and $i.NextRunTime.Year -gt 1999) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }
        [pscustomobject]@{ Job = $j.Name; State = $t.State; LastRun = $last; Result = (Get-ResultText $i.LastTaskResult); NextRun = $next; Missed = $i.NumberOfMissedRuns }
    }
    $rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
    if (Test-Path -LiteralPath $LogPath) {
        Write-Host "Last log lines ($LogPath):" -ForegroundColor Cyan
        Get-Content -LiteralPath $LogPath -Tail 8 | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host "No log yet at $LogPath"
    }
}

# --------------------------------------------------------------- status -----
if ($Status) { Show-Status; return }

# ------------------------------------------------------------ uninstall -----
if ($Uninstall) {
    $existing = @(Get-ScheduledTask -TaskPath $folder -ErrorAction SilentlyContinue)
    foreach ($t in $existing) {
        if ($PSCmdlet.ShouldProcess($t.TaskName, 'Unregister scheduled task')) {
            Unregister-ScheduledTask -TaskPath $folder -TaskName $t.TaskName -Confirm:$false
            Write-Host "removed $($t.TaskName)"
        }
    }
    if ($existing.Count -gt 0 -and -not $WhatIfPreference) {
        try {
            $svc = New-Object -ComObject Schedule.Service; $svc.Connect()
            $svc.GetFolder('\').DeleteFolder($folder.Trim('\'), 0)
        } catch { }
    }
    Write-Host "Removed $($existing.Count) task(s). The runner ($RunnerPath) and the log ($LogPath) were left in place."
    return
}

# ------------------------------------------------------------- show xml -----
# Print the Task Scheduler definition of every job and register nothing: for auditing
# what will be installed, and for the tests.
if ($ShowXml) {
    if (-not $BackendPath) { $BackendPath = 'C:\monitra\backend' }
    foreach ($j in $Jobs) {
        Write-Output ('### ' + (Get-TaskName $j.Name))
        Write-Output (New-TaskXml -Job $j -Mode 'System' -UserId '')
    }
    return
}

# -------------------------------------------------------------- install -----
if (-not $BackendPath) { throw 'Give -BackendPath, the backend folder that holds .env (for example D:\monitra\backend).' }
if (-not (Test-Path -LiteralPath $BackendPath)) { throw "BackendPath '$BackendPath' does not exist." }
if (-not (Test-Path -LiteralPath $RunnerSource)) { throw "Monitra-Job.ps1 must sit next to this script ($RunnerSource)." }
$BackendPath = (Resolve-Path -LiteralPath $BackendPath).Path

$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($RunOnlyWhenLoggedOn) {
    $mode = 'Interactive'
} elseif ($RunAsUser -match '^(SYSTEM|NT AUTHORITY\\SYSTEM)$') {
    $mode = 'System'
} else {
    $mode = 'Password'
}
if ($mode -ne 'Interactive' -and -not (Test-Administrator)) {
    throw 'Run this from an elevated PowerShell (Run as administrator).'
}

$credential = $null
if ($mode -eq 'Password') {
    $credential = Get-Credential -UserName $RunAsUser -Message "Password for $RunAsUser (handed to Task Scheduler, stored nowhere else)"
    if (-not $credential) { throw 'No credential given.' }
}

Write-Host "Installing Monitra scheduled jobs" -ForegroundColor Cyan
Write-Host "  backend   : $BackendPath  (port $Port)"
Write-Host "  runs as   : $(if ($mode -eq 'System') { 'SYSTEM' } elseif ($mode -eq 'Interactive') { $userId + ' (only while logged on)' } else { $RunAsUser })"
Write-Host "  tasks in  : $folder"

if ($PSCmdlet.ShouldProcess($InstallDir, 'Copy the runner')) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -LiteralPath $RunnerSource -Destination $RunnerPath -Force
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null

    # The runner is executed by a privileged account: nobody else may be able to edit it.
    $grants = @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
    if ($mode -eq 'Interactive') { $grants += "${userId}:(OI)(CI)F" }
    if ($mode -eq 'Password') { $grants += "$($credential.UserName):(OI)(CI)RX" }
    & icacls.exe $InstallDir /inheritance:r /grant:r @grants | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not tighten the permissions on $InstallDir (icacls exit $LASTEXITCODE)." }
    if ($mode -eq 'Password') {
        & icacls.exe (Split-Path -Parent $LogPath) /grant "$($credential.UserName):(OI)(CI)M" | Out-Null
    }
}

if (-not $SkipCheck -and -not $WhatIfPreference) {
    Write-Host "Checking the token and the backend ..." -ForegroundColor Cyan
    & $PowerShellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $RunnerPath -Job email-dispatch `
        -BackendPath $BackendPath -BaseUrl "http://127.0.0.1:$Port" -Check
    if ($LASTEXITCODE -ne 0) {
        throw 'The check failed (see above): fix the token or the backend, or pass -SkipCheck to install anyway.'
    }
}

foreach ($j in $Jobs) {
    $name = Get-TaskName $j.Name
    $xml = New-TaskXml -Job $j -Mode $mode -UserId $userId
    if ($PSCmdlet.ShouldProcess("$folder$name", "Register ($($j.Cron) UTC)")) {
        $register = @{ TaskName = $name; TaskPath = $folder; Xml = $xml; Force = $true }
        if ($mode -eq 'Password') {
            $register.User = $credential.UserName
            $register.Password = $credential.GetNetworkCredential().Password
        }
        Register-ScheduledTask @register | Out-Null
        Write-Host ("  registered {0,-26} {1}" -f $j.Name, $j.Cron)
    }
}
$credential = $null

if ($WhatIfPreference) { return }
Write-Host ''
Show-Status

if ($RunNow) {
    Write-Host "Running email-dispatch once ..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskPath $folder -TaskName (Get-TaskName 'email-dispatch')
    for ($waited = 0; $waited -lt 90; $waited += 2) {
        Start-Sleep -Seconds 2
        if ((Get-ScheduledTask -TaskPath $folder -TaskName (Get-TaskName 'email-dispatch')).State -ne 'Running') { break }
    }
    $info = Get-ScheduledTaskInfo -TaskPath $folder -TaskName (Get-TaskName 'email-dispatch')
    Write-Host ("email-dispatch result: {0} ({1})" -f $info.LastTaskResult, (Get-ResultText $info.LastTaskResult))
    if (Test-Path -LiteralPath $LogPath) { Get-Content -LiteralPath $LogPath -Tail 3 | ForEach-Object { Write-Host "  $_" } }
}
Write-Host ''
Write-Host "Done. Check later with:  .\Install-MonitraJobs.ps1 -Status"

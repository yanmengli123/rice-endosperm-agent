param(
    [int]$BridgePort = 52801,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$daemonTask = "Yuxi BrowserSkill Daemon"
$bridgeTask = "Yuxi BrowserSkill Bridge"

if ($Uninstall) {
    foreach ($taskName in @($bridgeTask, $daemonTask)) {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
    }
    return
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $repoRoot
$bridgeScript = Join-Path $PSScriptRoot "browser_skill_bridge.py"
$secretFile = Join-Path $workspaceRoot "tmp\browser-skill-bridge\secret"
$bsk = Join-Path $env:USERPROFILE ".local\bin\bsk.exe"
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source

foreach ($path in @($bsk, $pythonw, $bridgeScript)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required BrowserSkill component not found: $path"
    }
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
# Task Scheduler does not apply RestartOnFailure when a process is explicitly
# terminated (0xC000013A), which can happen during browser/E2E cleanup.  The
# minute trigger is a self-healing safety net: IgnoreNew makes it a no-op while
# the long-running task is healthy and starts it again after an external kill.
$recoveryTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddSeconds(10) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$triggers = @($logonTrigger, $recoveryTrigger)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$daemonAction = New-ScheduledTaskAction -Execute $bsk -Argument "daemon start --foreground"
Register-ScheduledTask `
    -TaskName $daemonTask `
    -Action $daemonAction `
    -Trigger $triggers `
    -Principal $principal `
    -Settings $settings `
    -Description "Keeps the local BrowserSkill daemon available for Yuxi; recovers within one minute after an external termination." `
    -Force | Out-Null

$bridgeArguments = (
    "`"$bridgeScript`" --host 0.0.0.0 --port $BridgePort " +
    "--bsk `"$bsk`" --secret-file `"$secretFile`" --session-ttl-s 900"
)
$bridgeAction = New-ScheduledTaskAction -Execute $pythonw -Argument $bridgeArguments
Register-ScheduledTask `
    -TaskName $bridgeTask `
    -Action $bridgeAction `
    -Trigger $triggers `
    -Principal $principal `
    -Settings $settings `
    -Description "Authenticated host bridge from Yuxi Docker services to BrowserSkill; recovers within one minute after an external termination." `
    -Force | Out-Null

if (-not (Get-NetTCPConnection -LocalPort 52800 -State Listen -ErrorAction SilentlyContinue)) {
    Start-ScheduledTask -TaskName $daemonTask
}
if (-not (Get-NetTCPConnection -LocalPort $BridgePort -State Listen -ErrorAction SilentlyContinue)) {
    Start-ScheduledTask -TaskName $bridgeTask
}

Write-Host "BrowserSkill tasks installed with one-minute self-healing: $daemonTask, $bridgeTask"

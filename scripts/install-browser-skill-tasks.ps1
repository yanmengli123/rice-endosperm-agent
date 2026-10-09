param(
    [int]$BridgePort = 52801,
    [int]$DaemonPort = 52800,
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
$daemonLauncher = Join-Path $PSScriptRoot "bsk_daemon_launcher.py"
$bridgeStateDir = Join-Path $workspaceRoot "tmp\browser-skill-bridge"
$secretFile = Join-Path $bridgeStateDir "secret"
# 无窗口守护进程的 stdout/stderr 落盘:CREATE_NO_WINDOW 下不重定向就永远丢失。
$daemonLogFile = Join-Path $bridgeStateDir "daemon.log"
$bsk = Join-Path $env:USERPROFILE ".local\bin\bsk.exe"
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source

foreach ($path in @($bsk, $pythonw, $bridgeScript, $daemonLauncher)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required BrowserSkill component not found: $path"
    }
}
New-Item -ItemType Directory -Force -Path $bridgeStateDir | Out-Null

$user = "$env:USERDOMAIN\$env:USERNAME"
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
# Task Scheduler does not apply RestartOnFailure when a process is explicitly
# terminated (0xC000013A), which can happen during browser/E2E cleanup.  The
# minute trigger is a self-healing safety net: IgnoreNew makes it a no-op while
# the long-running task is healthy and starts it again after an external kill.
# RepetitionDuration must be explicit: on older Windows/PowerShell the default
# repetition window is 1 hour, after which self-healing silently stops.
# 注意:[TimeSpan]::MaxValue 会序列化成 P99999999DT23H59M59S,超出任务计划
# XML 取值范围,Register-ScheduledTask 直接拒绝注册;用 10 年近似「无限」。
$recoveryTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddSeconds(10) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))
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

# bsk daemon start 只能前台运行;直接执行 bsk.exe 会在 InteractiveToken 会话弹
# 出常驻控制台窗口,且被自恢复触发器反复拉起。改经 pythonw + CREATE_NO_WINDOW
# 启动器隐藏运行,退出码原样透传,自恢复语义不变。
$daemonArguments = "`"$daemonLauncher`" --bsk `"$bsk`" --log `"$daemonLogFile`" -- --foreground"
$daemonAction = New-ScheduledTaskAction -Execute $pythonw -Argument $daemonArguments
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

if (-not (Get-NetTCPConnection -LocalPort $DaemonPort -State Listen -ErrorAction SilentlyContinue)) {
    Start-ScheduledTask -TaskName $daemonTask
}
if (-not (Get-NetTCPConnection -LocalPort $BridgePort -State Listen -ErrorAction SilentlyContinue)) {
    Start-ScheduledTask -TaskName $bridgeTask
}

Write-Host "BrowserSkill tasks installed with one-minute self-healing: $daemonTask, $bridgeTask"

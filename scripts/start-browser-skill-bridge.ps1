param(
    [string]$WorkspaceRoot = "D:\yuxi",
    [int]$Port = 52801
)

$ErrorActionPreference = "Stop"
$bsk = Join-Path $env:USERPROFILE ".local\bin\bsk.exe"
$bridge = Join-Path $WorkspaceRoot "Yuxi\scripts\browser_skill_bridge.py"
$secretFile = Join-Path $WorkspaceRoot "tmp\browser-skill-bridge\secret"

if (-not (Test-Path -LiteralPath $bsk)) {
    throw "bsk.exe not found: $bsk"
}
if (-not (Test-Path -LiteralPath $bridge)) {
    throw "BrowserSkill bridge not found: $bridge"
}

$bridgeListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($bridgeListener) {
    exit 0
}

$listener = Get-NetTCPConnection -LocalPort 52800 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    Start-Process -FilePath $bsk -ArgumentList @("daemon", "start", "--foreground") -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        $listener = Get-NetTCPConnection -LocalPort 52800 -State Listen -ErrorAction SilentlyContinue
    } until ($listener -or (Get-Date) -ge $deadline)
    if (-not $listener) {
        throw "bsk daemon did not listen on 127.0.0.1:52800"
    }
}

$env:BSK_AUTO_START = "0"
python $bridge --host 0.0.0.0 --port $Port --bsk $bsk --secret-file $secretFile

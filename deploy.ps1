# Starts a new Docker deployment from a Git clone or GitHub ZIP on Windows.
[CmdletBinding()]
param(
    [string]$DataDir,
    [ValidatePattern('^[a-z0-9][a-z0-9_-]{0,26}$')][string]$Project = 'yuxi-beta5',
    [ValidateRange(1, 65535)][int]$WebPort = 5180,
    [ValidateRange(1, 65535)][int]$GatewayPort = 9089,
    [ValidatePattern('^[a-zA-Z0-9.-]+(:[0-9]+)?$')][string]$DockerHubRegistry = 'docker.io',
    [switch]$Validate
)

$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
if (!$DataDir) {
    $defaultRoot = [Environment]::GetFolderPath('LocalApplicationData')
    $DataDir = Join-Path (Join-Path $defaultRoot 'Yuxi') $Project
}
$data = [IO.Path]::GetFullPath($DataDir)
$sourcePrefix = $source.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if ($data -eq $source -or $data.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Choose a data directory outside the downloaded source folder.'
}
if ($WebPort -eq $GatewayPort) { throw 'The web and gateway ports must differ.' }

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE)." }
}

function New-RandomHex {
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    return [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
}

if (!$Validate) { Invoke-Docker info --format '{{.ServerVersion}}' }
$privateDir = Join-Path $data 'config'
$runtimeConfig = Join-Path $privateDir 'docker'
$volumes = Join-Path $data 'volumes'
$envFile = Join-Path $privateDir '.env.prod'
New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
if ($env:OS -eq 'Windows_NT') {
    $ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls $privateDir /inheritance:r /grant:r "*${ownerSid}:(OI)(CI)(F)" '*S-1-5-18:(OI)(CI)(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the configuration directory.' }
}
if (!(Test-Path -LiteralPath $envFile)) {
    if (Test-Path -LiteralPath $volumes) { throw 'Existing data requires its original configuration and encryption key.' }
    $secrets = [ordered]@{
        POSTGRES_PASSWORD = (New-RandomHex)
        MINIO_ACCESS_KEY = ('yuxi' + (New-RandomHex).Substring(0, 16))
        MINIO_SECRET_KEY = (New-RandomHex)
        NEO4J_PASSWORD = (New-RandomHex)
        JWT_SECRET_KEY = (New-RandomHex)
        YUXI_SECRET_MASTER_KEY = (New-RandomHex)
        SANDBOX_PROVISIONER_TOKEN = (New-RandomHex)
        YUXI_INSTANCE_ID = $Project
    }
    $config = Get-Content -LiteralPath (Join-Path $source '.env.template') -Raw
    foreach ($name in $secrets.Keys) {
        $config = [regex]::Replace($config, "(?m)^${name}=[^\r\n]*", "$name=$($secrets[$name])")
    }
    $stream = [IO.File]::Open($envFile, [IO.FileMode]::CreateNew)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($config)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
}

foreach ($relative in @('apisix/config.yaml', 'apisix/apisix.yaml', 'sandbox_provisioner/sandbox.env')) {
    $target = Join-Path $runtimeConfig $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path (Join-Path $source 'docker') $relative) -Destination $target
}

# Keep the generated settings scoped to this script and its Docker child processes.
$env:YUXI_DATA_DIR = $volumes.Replace('\', '/')
$env:YUXI_DEPLOY_CONFIG_DIR = $runtimeConfig.Replace('\', '/')
$env:YUXI_PROD_ENV_FILE = $envFile.Replace('\', '/')
$env:YUXI_CONTAINER_PREFIX = "$Project-"
$env:YUXI_VERSION = $Project
$env:YUXI_WEB_BIND = '127.0.0.1'
$env:YUXI_WEB_PORT = "$WebPort"
$env:APISIX_HTTP_BIND = '127.0.0.1'
$env:APISIX_HTTP_PORT = "$GatewayPort"
$env:YUXI_PUBLIC_WEB_URL = "http://127.0.0.1:$WebPort"
$env:SANDBOX_DOCKER_NETWORK_PREFIX = "$Project-net"
$env:SANDBOX_DOCKER_SANDBOX_PREFIX = "$Project-sandbox"
$env:YUXI_MILVUS_IMAGE = 'milvusdb/milvus:v2.5.17'
$env:YUXI_DOCKERHUB_REGISTRY = $DockerHubRegistry
$revision = $null
if ((Test-Path -LiteralPath (Join-Path $source '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
    $revision = & git -C $source rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Could not identify source revision.' }
}
$env:YUXI_DEPLOYMENT_REVISION = $revision
$compose = @('compose', '--project-directory', $source, '--env-file', $envFile,
    '-p', $Project, '-f', (Join-Path $source 'docker-compose.prod.yml'))
Invoke-Docker @compose config --quiet
if ($Validate) {
    Write-Host "Deployment configuration is valid. Configuration and data location: $data"
    return
}

# Build the backend once. Worker and channels use the same image.
Invoke-Docker @compose build api web sandbox-provisioner
Invoke-Docker @compose up --detach --no-build --wait --wait-timeout 600
$ready = Invoke-RestMethod -Uri "http://127.0.0.1:$WebPort/api/system/ready" -TimeoutSec 15
if ($ready.status -ne 'ready') { throw 'The application has not passed readiness.' }
Write-Host "Yuxi Web: http://127.0.0.1:$WebPort"
Write-Host "API gateway: http://127.0.0.1:$GatewayPort"
Write-Host "Persistent data and configuration: $data"
Write-Host 'On a new installation, create an administrator in the browser and configure a model provider.'

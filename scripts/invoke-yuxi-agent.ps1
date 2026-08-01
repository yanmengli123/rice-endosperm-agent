[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$Question,

    [string]$BaseUrl = "http://localhost:9088",
    [string]$AgentSlug = "default-chatbot",
    [string]$ThreadId,
    [string]$RequestId = ([guid]::NewGuid().ToString()),
    [ValidateRange(1, 60)]
    [int]$PollIntervalSeconds = 2,
    [ValidateRange(10, 3600)]
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"

$apiKey = [Environment]::GetEnvironmentVariable("YUXI_API_KEY")
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "YUXI_API_KEY is required. Load it from a secure prompt; do not store it in this script."
}
if (-not $apiKey.StartsWith("yxkey_", [StringComparison]::Ordinal)) {
    throw "YUXI_API_KEY has an invalid format."
}
if ($RequestId.Length -gt 64) {
    throw "RequestId cannot exceed 64 characters."
}

$base = $BaseUrl.TrimEnd("/")
$headers = @{
    Authorization = "Bearer $apiKey"
    Accept = "application/json"
    "X-Client-Request-ID" = $RequestId
}
$payload = [ordered]@{
    agent_slug = $AgentSlug
    messages = @(
        [ordered]@{
            role = "user"
            content = $Question
        }
    )
    request_id = $RequestId
    async_mode = $true
}
if (-not [string]::IsNullOrWhiteSpace($ThreadId)) {
    $payload.thread_id = $ThreadId
}

$run = Invoke-RestMethod `
    -Method Post `
    -Uri "$base/api/agent-invocation/agent-call/runs" `
    -Headers $headers `
    -ContentType "application/json; charset=utf-8" `
    -Body ($payload | ConvertTo-Json -Depth 8 -Compress)

if ([string]::IsNullOrWhiteSpace([string]$run.run_id)) {
    throw "Yuxi did not return a run_id."
}

Write-Host "Run created: run_id=$($run.run_id), thread_id=$($run.thread_id), request_id=$($run.request_id)"

$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
$terminalStatuses = @("completed", "failed", "cancelled", "interrupted")
$result = $run

while ($terminalStatuses -notcontains [string]$result.status) {
    if ([DateTimeOffset]::UtcNow -ge $deadline) {
        throw "Timed out while waiting. Keep run_id=$($run.run_id) and query it again; do not create a duplicate run."
    }

    Start-Sleep -Seconds $PollIntervalSeconds
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri "$base/api/agent-invocation/agent-call/runs/result" `
        -Headers $headers `
        -ContentType "application/json; charset=utf-8" `
        -Body (@{
            run_id = $run.run_id
            agent_slug = $AgentSlug
        } | ConvertTo-Json -Compress)
}

if ($result.status -ne "completed") {
    $detail = if ($result.error) { [string]$result.error } else { "No error detail was returned." }
    throw "Run ended with status $($result.status): $detail"
}

$result

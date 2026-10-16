$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envFile = Join-Path $repoRoot ".env"
$ngrokConfig = Join-Path $PSScriptRoot "ngrok.yml"
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw "Copy .env.example to .env and configure it before registering startup tasks."
}
if (-not (Test-Path -LiteralPath $ngrokConfig -PathType Leaf)) {
    throw "Copy ngrok.yml.example to ngrok.yml and configure your reserved domain first."
}

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $python = $pythonCommand.Source
}
$ngrok = (Get-Command ngrok -ErrorAction Stop).Source
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$trigger = New-ScheduledTaskTrigger -AtStartup -Delay (New-TimeSpan -Seconds 30)

$serverAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$repoRoot\server.py`" http" `
    -WorkingDirectory $repoRoot
$tunnelAction = New-ScheduledTaskAction `
    -Execute $ngrok `
    -Argument "start --config `"$ngrokConfig`" pc-files" `
    -WorkingDirectory $repoRoot

Register-ScheduledTask `
    -TaskName "MCP File Server" `
    -Action $serverAction `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Start the sandboxed MCP file server at boot." `
    -Force | Out-Null
Register-ScheduledTask `
    -TaskName "MCP ngrok Tunnel" `
    -Action $tunnelAction `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Start the configured ngrok tunnel at boot." `
    -Force | Out-Null

Write-Output "Registered startup tasks for the MCP server and ngrok."

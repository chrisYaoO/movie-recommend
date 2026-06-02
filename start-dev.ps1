param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$frontend = Join-Path $root "frontend"
$nodeModules = Join-Path $frontend "node_modules"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing virtualenv Python at $python. Create the venv and install requirements first."
}

if (-not (Test-Path -LiteralPath $nodeModules)) {
    throw "Missing frontend dependencies at $nodeModules. Run: npm --prefix frontend install"
}

$backendCommand = @"
Set-Location -LiteralPath '$root'
& '$python' -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port $BackendPort
"@

$frontendCommand = @"
Set-Location -LiteralPath '$frontend'
npm run dev -- --host 127.0.0.1 --port $FrontendPort
"@

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $backendCommand
) -WorkingDirectory $root

Start-Sleep -Seconds 1

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $frontendCommand
) -WorkingDirectory $frontend

$frontendUrl = "http://127.0.0.1:$FrontendPort/"
$backendUrl = "http://127.0.0.1:$BackendPort/"

Write-Host "Backend starting at $backendUrl"
Write-Host "Frontend starting at $frontendUrl"
Write-Host "Close the opened PowerShell windows to stop the services."

if ($OpenBrowser) {
    Start-Process $frontendUrl
}

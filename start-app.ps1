param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = Join-Path $root "desktop"
$electron = Join-Path $desktop "node_modules\electron"
$frontendDist = Join-Path $root "frontend\dist\index.html"

if (-not (Test-Path -LiteralPath $electron)) {
    throw "Missing Electron dependencies at $electron. Run: npm --prefix desktop install"
}

if (-not (Test-Path -LiteralPath $frontendDist)) {
    Write-Host "Missing built frontend at $frontendDist. Building it now..."
    Push-Location -LiteralPath (Join-Path $root "frontend")
    try {
        npm run build
    } finally {
        Pop-Location
    }
}

$env:MOVIES_BACKEND_PORT = "$BackendPort"
$env:MOVIES_FRONTEND_PORT = "$FrontendPort"
$launcher = Join-Path $desktop "launch.cjs"

Start-Process -WindowStyle Hidden -FilePath "node.exe" -ArgumentList @(
    "`"$launcher`""
) -WorkingDirectory $root

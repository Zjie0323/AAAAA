$PYTHON = "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if (-not (Test-Path $PYTHON)) {
    Write-Host "WorkBuddy Python not found: $PYTHON" -ForegroundColor Red
    exit 1
}
$PORT = if ($args[0]) { $args[0] } else { 8000 }
Write-Host "Starting A-stock realtime board at http://localhost:$PORT"
& $PYTHON "$PSScriptRoot\server.py" $PORT

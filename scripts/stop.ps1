$Root = Split-Path -Parent $PSScriptRoot
$PidPath = Join-Path $Root "logs\studio.pid"
if (-not (Test-Path $PidPath)) {
    Write-Host "irodori voice studio is not running."
    exit 0
}
$studioPid = [int](Get-Content $PidPath -Raw)
$process = Get-Process -Id $studioPid -ErrorAction SilentlyContinue
if ($process) { Stop-Process -Id $studioPid -Force }
Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
Write-Host "irodori voice studio stopped. audio.cpp is still running."

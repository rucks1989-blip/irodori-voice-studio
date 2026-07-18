$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root "runtime"
$Exe = Join-Path $Runtime "audio_cpp\audiocpp_server.exe"
$Config = Join-Path $Runtime "server.json"
$PidPath = Join-Path $Root "logs\audio-cpp.pid"
$Stdout = Join-Path $Root "logs\audio-cpp.stdout.log"
$Stderr = Join-Path $Root "logs\audio-cpp.stderr.log"

try {
    if ((Invoke-WebRequest "http://127.0.0.1:8091/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { exit 0 }
} catch { }
if (-not (Test-Path $Exe)) { throw "audio.cpp runtime is missing. Run setup.bat." }
if (-not (Test-Path $Config)) { throw "audio.cpp configuration is missing. Run setup.bat." }
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
$process = Start-Process -FilePath $Exe -ArgumentList @("--config", $Config) -WorkingDirectory (Split-Path $Exe) -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -WindowStyle Hidden -PassThru
$process.Id | Set-Content $PidPath -Encoding ascii
$deadline = [DateTime]::UtcNow.AddSeconds(150)
while ([DateTime]::UtcNow -lt $deadline) {
    if ($process.HasExited) {
        if (Test-Path $Stderr) { Get-Content $Stderr -Tail 50 }
        throw "audio.cpp exited before becoming ready."
    }
    try {
        if ((Invoke-WebRequest "http://127.0.0.1:8091/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { exit 0 }
    } catch { }
    Start-Sleep -Milliseconds 500
}
throw "Timed out while waiting for audio.cpp."

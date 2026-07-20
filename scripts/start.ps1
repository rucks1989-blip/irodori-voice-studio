$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PidPath = Join-Path $Root "logs\studio.pid"
$Stdout = Join-Path $Root "logs\studio.stdout.log"
$Stderr = Join-Path $Root "logs\studio.stderr.log"
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null

$SetupState = Join-Path $Root "data\setup-state.json"
$setupComplete = $false
if (Test-Path $SetupState) {
    try {
        $setupInfo = Get-Content $SetupState -Raw -Encoding UTF8 | ConvertFrom-Json
        $setupComplete = [bool]$setupInfo.completed
        if ($setupInfo.backend -in @("cpu", "cuda")) {
            $required = @(
                (Join-Path $Root "runtime\audio_cpp\audiocpp_server.exe"),
                (Join-Path $Root "runtime\server.json"),
                (Join-Path $Root "models\irodori-tts-GGUF\irodori-tts-500m-v3-q8_0.gguf")
            )
            if ($required | Where-Object { -not (Test-Path $_) }) { $setupComplete = $false }
        }
    } catch { $setupComplete = $false }
}
if (-not $setupComplete) {
    Write-Host "初回セットアップが完了していません。setup.batを開始します。"
    & cmd.exe /d /c "call `"$Root\setup.bat`""
    try { $setupComplete = [bool]((Get-Content $SetupState -Raw -Encoding UTF8 | ConvertFrom-Json).completed) } catch { $setupComplete = $false }
    if ($LASTEXITCODE -ne 0 -or -not $setupComplete) {
        throw "初回セットアップが完了していません。setup.batの日本語案内に従ってください。"
    }
}

if (Test-Path $PidPath) {
    $oldPid = [int](Get-Content $PidPath -Raw)
    if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
        Start-Process "http://127.0.0.1:6670"
        exit 0
    }
    Remove-Item $PidPath -Force
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
$pythonPath = if ($python) { $python.Source } else { $null }
if (-not $python) {
    $localPython = Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"
    if (Test-Path $localPython) { $pythonPath = $localPython }
}
if (-not $pythonPath) { throw "Python 3が見つかりません。" }

function Test-Port([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return $task.Wait(500) -and $client.Connected
    } catch { return $false } finally { $client.Dispose() }
}
if (Test-Port 6666) { throw "API用の6666番ポートが使用中です。先にほかのプログラムを終了してください。" }
if (Test-Port 6670) { throw "UI用の6670番ポートが使用中です。先にほかのプログラムを終了してください。" }

try {
    & $pythonPath -c "import requests,numpy,soundfile" 2>$null
} catch { }
if ($LASTEXITCODE -ne 0) {
    Write-Host "必要なPythonパッケージをインストールしています。"
    & $pythonPath -m pip install -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "必要なPythonパッケージのインストールに失敗しました。" }
}

$process = Start-Process -FilePath $pythonPath -ArgumentList @("-X", "utf8", (Join-Path $Root "app.py")) -WorkingDirectory $Root -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -WindowStyle Hidden -PassThru
$process.Id | Set-Content -Path $PidPath -Encoding ascii
$deadline = [DateTime]::UtcNow.AddSeconds(30)
while ([DateTime]::UtcNow -lt $deadline) {
    if ($process.HasExited) {
        if (Test-Path $Stderr) { Get-Content $Stderr -Tail 30 }
        throw "irodori voice studioが起動完了前に終了しました。"
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:6670/api/health" -TimeoutSec 2
        if ($health.studio -eq "ok") {
            Start-Process "http://127.0.0.1:6670"
            Write-Host "irodori voice studioを起動しました。"
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 300
}
throw "UIの起動待機がタイムアウトしました。"


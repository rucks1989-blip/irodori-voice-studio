$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Root "logs"
$Runs = Join-Path $Logs "runs"
$PidPath = Join-Path $Logs "studio.pid"
$CurrentPath = Join-Path $Logs "studio-current.json"
$LockPath = Join-Path $Logs "studio.start.lock"
New-Item -ItemType Directory -Force -Path $Runs | Out-Null

$processPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
[Environment]::SetEnvironmentVariable("Path", $null, "Process")
[Environment]::SetEnvironmentVariable("PATH", $processPath, "Process")

function Test-StudioHealth {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:6670/api/health" -TimeoutSec 2
        return $health.studio -eq "ok"
    } catch {
        return $false
    }
}

function Open-Studio {
    if ($env:IRODORI_NO_BROWSER -ne "1") {
        Start-Process "http://127.0.0.1:6670"
    }
}

$lock = $null
try {
    try {
        $lock = [IO.File]::Open($LockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        for ($i = 0; $i -lt 40; $i++) {
            if (Test-StudioHealth) {
                Open-Studio
                Write-Host "irodori voice studioは既に起動しています。http://127.0.0.1:6670"
                exit 0
            }
            Start-Sleep -Milliseconds 250
        }
        throw "irodori voice studioの起動処理が既に実行中です。"
    }

    if (Test-StudioHealth) {
        Open-Studio
        Write-Host "irodori voice studioは既に起動しています。http://127.0.0.1:6670"
        exit 0
    }

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
                if ($required | Where-Object { -not (Test-Path $_ -PathType Leaf) }) { $setupComplete = $false }
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

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    $pythonPath = if ($python) { $python.Source } else { $null }
    if (-not $pythonPath) {
        $localPython = Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"
        if (Test-Path $localPython -PathType Leaf) { $pythonPath = $localPython }
    }
    if (-not $pythonPath) { throw "Python 3が見つかりません。" }

    if (Test-Path $PidPath) {
        $oldPid = 0
        [void][int]::TryParse((Get-Content $PidPath -Raw -Encoding ASCII).Trim(), [ref]$oldPid)
        $oldProcess = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        $owned = $false
        if ($oldProcess) {
            try { $owned = [IO.Path]::GetFullPath($oldProcess.Path) -eq [IO.Path]::GetFullPath($pythonPath) } catch { $owned = $false }
        }
        if ($owned) {
            throw "このプロジェクトのPythonプロセスは存在しますが、UIのhealth checkに応答しません。PID: $oldPid"
        }
        Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
    }

    function Test-Port([int]$Port) {
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $task = $client.ConnectAsync("127.0.0.1", $Port)
            return $task.Wait(500) -and $client.Connected
        } catch { return $false } finally { $client.Dispose() }
    }
    if (Test-Port 6666) { throw "API用の6666番ポートが別のプロセスにより使用中です。自動終了は行いません。" }
    if (Test-Port 6670) { throw "UI用の6670番ポートが別のプロセスにより使用中です。自動終了は行いません。" }

    & $pythonPath -c "import requests,numpy,soundfile" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "必要なPythonパッケージをプロジェクトの既存環境へインストールします。"
        & $pythonPath -m pip install -r (Join-Path $Root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { throw "必要なPythonパッケージのインストールに失敗しました。" }
    }

    $runId = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $Stdout = Join-Path $Runs "studio_$runId.stdout.log"
    $Stderr = Join-Path $Runs "studio_$runId.stderr.log"
    $process = Start-Process -FilePath $pythonPath -ArgumentList @("-X", "utf8", (Join-Path $Root "app.py")) -WorkingDirectory $Root -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -WindowStyle Hidden -PassThru
    $process.Id | Set-Content -Path $PidPath -Encoding ASCII
    $current = @{
        run_id = $runId
        pid = $process.Id
        parent_pid = $PID
        executable = $pythonPath
        command = "`"$pythonPath`" -X utf8 `"$Root\app.py`""
        ports = @(6666, 6670)
        started_at = [DateTimeOffset]::Now.ToString("o")
        stdout = $Stdout
        stderr = $Stderr
    } | ConvertTo-Json
    [IO.File]::WriteAllText($CurrentPath, $current + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) {
            if (Test-Path $Stderr) { Get-Content $Stderr -Tail 30 -Encoding UTF8 }
            throw "irodori voice studioが起動完了前に終了しました。終了コード: $($process.ExitCode)"
        }
        if (Test-StudioHealth) {
            Open-Studio
            Write-Host "irodori voice studioを起動しました。PID: $($process.Id)"
            exit 0
        }
        Start-Sleep -Milliseconds 300
    }
    throw "UIの起動待機がタイムアウトしました。"
} finally {
    if ($lock) { $lock.Dispose() }
}

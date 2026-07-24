$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root "runtime"
$Exe = Join-Path $Runtime "audio_cpp\audiocpp_server.exe"
$Config = Join-Path $Runtime "server.json"
$Logs = Join-Path $Root "logs"
$Runs = Join-Path $Logs "runs"
$PidPath = Join-Path $Logs "audio-cpp.pid"
$CurrentPath = Join-Path $Logs "audio-cpp-current.json"
$LockPath = Join-Path $Logs "audio-cpp.start.lock"
New-Item -ItemType Directory -Force -Path $Runs | Out-Null

# Windows環境でPath/PATHが重複している場合のStart-Process例外を、このプロセス内だけで回避する。
$processPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
[Environment]::SetEnvironmentVariable("Path", $null, "Process")
[Environment]::SetEnvironmentVariable("PATH", $processPath, "Process")

function Test-Health {
    try {
        return (Invoke-WebRequest "http://127.0.0.1:8091/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-OwnedProcess([int]$ProcessId) {
    if ($ProcessId -le 0) { return $false }
    $candidate = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $candidate) { return $false }
    try {
        return [IO.Path]::GetFullPath($candidate.Path) -eq [IO.Path]::GetFullPath($Exe)
    } catch {
        return $false
    }
}

$lock = $null
try {
    try {
        $lock = [IO.File]::Open($LockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        for ($i = 0; $i -lt 40; $i++) {
            if (Test-Health) {
                Write-Host "audio.cppは既に起動しています。"
                exit 0
            }
            Start-Sleep -Milliseconds 250
        }
        throw "audio.cppの起動処理が既に実行中です。しばらく待ってから再度実行してください。"
    }

    if (Test-Health) {
        Write-Host "audio.cppは既に起動しています。"
        exit 0
    }
    if (Test-Path $PidPath) {
        $oldPid = 0
        [void][int]::TryParse((Get-Content $PidPath -Raw -Encoding ASCII).Trim(), [ref]$oldPid)
        if (Test-OwnedProcess $oldPid) {
            throw "このプロジェクトのaudio.cppプロセスは存在しますが、health checkに応答しません。PID: $oldPid"
        }
        Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path $Exe -PathType Leaf)) { throw "audio.cpp runtimeが見つかりません。setup.batを実行してください。" }
    if (-not (Test-Path $Config -PathType Leaf)) { throw "audio.cpp設定が見つかりません。setup.batを実行してください。" }

    $runId = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $Stdout = Join-Path $Runs "audio-cpp_$runId.stdout.log"
    $Stderr = Join-Path $Runs "audio-cpp_$runId.stderr.log"
    $TraceLog = Join-Path $Runs "audio-cpp_$runId.trace.log"
    $process = Start-Process -FilePath $Exe -ArgumentList @("--config", $Config, "--log", "--log-file", $TraceLog) -WorkingDirectory (Split-Path $Exe) -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -WindowStyle Hidden -PassThru
    $process.Id | Set-Content $PidPath -Encoding ASCII
    $current = @{
        run_id = $runId
        pid = $process.Id
        parent_pid = $PID
        executable = $Exe
        command = "`"$Exe`" --config `"$Config`" --log --log-file `"$TraceLog`""
        port = 8091
        started_at = [DateTimeOffset]::Now.ToString("o")
        stdout = $Stdout
        stderr = $Stderr
        trace = $TraceLog
    } | ConvertTo-Json
    [IO.File]::WriteAllText($CurrentPath, $current + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))

    $deadline = [DateTime]::UtcNow.AddSeconds(150)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) {
            if (Test-Path $Stderr) { Get-Content $Stderr -Tail 50 -Encoding UTF8 }
            throw "audio.cppが起動途中で終了しました。終了コード: $($process.ExitCode)"
        }
        if (Test-Health) {
            Write-Host "audio.cppを起動しました。PID: $($process.Id)"
            exit 0
        }
        Start-Sleep -Milliseconds 500
    }
    throw "audio.cppの起動待機がタイムアウトしました。"
} finally {
    if ($lock) { $lock.Dispose() }
}

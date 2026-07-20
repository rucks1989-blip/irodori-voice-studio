param([switch]$NoOpen)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$SupportRoot = Join-Path $Root "ai-support"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Work = Join-Path ([IO.Path]::GetTempPath()) "irodori-ai-support-$Stamp-$PID"
$Bundle = Join-Path $SupportRoot "AIに渡す診断情報-$Stamp.zip"
$PromptFile = Join-Path $SupportRoot "AIに貼り付ける文章-$Stamp.txt"
$RepositoryUrl = "https://github.com/rucks1989-blip/irodori-voice-studio"
$ExampleInstallRoot = "D:\AI\irodori-voice-studio"
$SourceExtensions = @(".bat", ".ps1", ".py", ".js", ".css", ".html", ".json", ".md", ".txt")
$BinaryExtensions = @(".wav", ".mp3", ".flac", ".safetensors", ".ckpt", ".pth", ".pt", ".gguf", ".onnx", ".bin", ".exe", ".dll", ".zip", ".7z")
$ExcludedFolders = @(".git", ".venv", "__pycache__", "ai-support")

function Write-Utf8([string]$Path, [string]$Text) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    Set-Content -LiteralPath $Path -Value $Text -Encoding UTF8
}
function Get-Relative([string]$Path) {
    $rootPrefix = [IO.Path]::GetFullPath($Root).TrimEnd("\") + "\"
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "プロジェクト外のパスです: $fullPath" }
    return $fullPath.Substring($rootPrefix.Length)
}
function Is-Excluded([string]$Path) {
    foreach ($part in ((Get-Relative $Path) -split '[\\/]')) { if ($part -in $ExcludedFolders) { return $true } }
    return $false
}
function Copy-Text([string]$Source, [string]$Destination, [int]$Tail = 0) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { return }
    $text = if ($Tail) { (Get-Content -LiteralPath $Source -Tail $Tail -ErrorAction SilentlyContinue) -join "`r`n" } else { Get-Content -LiteralPath $Source -Raw -ErrorAction SilentlyContinue }
    if ($null -eq $text) { $text = "" }
    $text = $text -replace '(?im)^([^\r\n]*(?:api[_-]?key|token|secret|password|authorization)[^:=\r\n]*[:=]\s*).+$', '${1}<redacted>'
    Write-Utf8 $Destination $text
}
function Protect-Secrets($Value) {
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        $protected = [ordered]@{}
        foreach ($key in $Value.Keys) {
            $protected[$key] = if ([string]$key -match '(?i)(api[_-]?key|token|secret|password|credential|authorization)') { '<redacted>' } else { Protect-Secrets $Value[$key] }
        }
        return $protected
    }
    if ($Value -is [pscustomobject]) {
        $protected = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) {
            $protected[$property.Name] = if ($property.Name -match '(?i)(api[_-]?key|token|secret|password|credential|authorization)') { '<redacted>' } else { Protect-Secrets $property.Value }
        }
        return [pscustomobject]$protected
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return @($Value | ForEach-Object { Protect-Secrets $_ })
    }
    return $Value
}
function Command-Result([string]$Name, [string[]]$Arguments = @()) {
    try { $command = Get-Command $Name -ErrorAction Stop; return (((& $command.Source @Arguments 2>&1) | Out-String).Trim()) }
    catch { return "取得できません: $($_.Exception.Message)" }
}
function Health-Result([string]$Name, [string]$Url) {
    try { return "$Name ($Url): 接続成功`r`n$((Invoke-RestMethod $Url -TimeoutSec 3) | ConvertTo-Json -Depth 8)" }
    catch { return "$Name ($Url): 接続失敗`r`n$($_.Exception.Message)" }
}

try {
    New-Item -ItemType Directory -Force -Path $SupportRoot, $Work | Out-Null
    $SourceOut = Join-Path $Work "source"
    $DiagnosticsOut = Join-Path $Work "diagnostics"
    New-Item -ItemType Directory -Force -Path $SourceOut, $DiagnosticsOut | Out-Null

    Get-ChildItem -LiteralPath $Root -Recurse -File -Force | ForEach-Object {
        if (Is-Excluded $_.FullName) { return }
        if ($_.Name -eq "settings.local.json" -or $_.Length -gt 5MB -or $_.Extension.ToLowerInvariant() -notin $SourceExtensions) { return }
        $destination = Join-Path $SourceOut (Get-Relative $_.FullName)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        if ($_.Extension.Equals(".json", [StringComparison]::OrdinalIgnoreCase)) {
            try {
                $safeJson = Protect-Secrets (Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json)
                Write-Utf8 $destination ($safeJson | ConvertTo-Json -Depth 20)
            } catch {
                Copy-Text $_.FullName $destination
            }
        } else {
            Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
        }
    }

    $settingsPath = Join-Path $Root "settings.local.json"
    if (Test-Path -LiteralPath $settingsPath) {
        try {
            $settings = Protect-Secrets (Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json)

            Write-Utf8 (Join-Path $DiagnosticsOut "settings.local.redacted.json") ($settings | ConvertTo-Json -Depth 12)
        } catch { Write-Utf8 (Join-Path $DiagnosticsOut "settings.local.read-error.txt") $_.Exception.Message }
    } else { Write-Utf8 (Join-Path $DiagnosticsOut "settings.local.status.txt") "settings.local.json は存在しません。" }

    $pythonPath = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    $environment = @(
        "irodori voice studio AI診断情報", "作成日時: $(Get-Date -Format o)", "実際のインストール先: $Root",
        "指示文で使用する配置例: $ExampleInstallRoot", "コンピューター名: $env:COMPUTERNAME", "Windowsユーザー名: $env:USERNAME",
        "OS: $([Environment]::OSVersion.VersionString)", "OS 64bit: $([Environment]::Is64BitOperatingSystem)", "PowerShell: $($PSVersionTable.PSVersion)",
        "", "[Python]", (Command-Result "python.exe" @("--version")), "python path: $pythonPath", "", "[NVIDIA GPU]",
        (Command-Result "nvidia-smi.exe" @("--query-gpu=name,driver_version,compute_cap,memory.total", "--format=csv,noheader")),
        "", "[関連Pythonパッケージ]", (Command-Result "python.exe" @("-m", "pip", "show", "numpy", "requests", "soundfile")),
        "", "[接続確認]", (Health-Result "UI" "http://127.0.0.1:6670/api/health"), (Health-Result "API" "http://127.0.0.1:6666/api/health"),
        (Health-Result "audio.cpp" "http://127.0.0.1:8091/health"), (Health-Result "会話用LLM" "http://127.0.0.1:11438/health")
    ) -join "`r`n"
    Write-Utf8 (Join-Path $DiagnosticsOut "environment.txt") $environment

    $inventory = Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { -not (Is-Excluded $_.FullName) } | ForEach-Object {
        [pscustomobject]@{
            RelativePath = Get-Relative $_.FullName; FullPath = $_.FullName; Extension = $_.Extension; Bytes = $_.Length
            LastWriteTime = $_.LastWriteTime.ToString("o")
            IncludedAsSource = ($_.Extension.ToLowerInvariant() -in $SourceExtensions -and $_.Length -le 5MB -and $_.Name -ne "settings.local.json")
            LargeOrBinaryExcluded = ($_.Extension.ToLowerInvariant() -in $BinaryExtensions -or $_.Length -gt 5MB)
        }
    }
    $inventory | Export-Csv (Join-Path $DiagnosticsOut "file-inventory.csv") -NoTypeInformation -Encoding UTF8

    @(
        @{S="data\setup-state.json";D="setup-state.json";T=0}, @{S="data\setup-result.html";D="setup-result.html";T=0},
        @{S="runtime\server.json";D="server.json";T=0}, @{S="logs\studio.stdout.log";D="studio.stdout.tail.log";T=1000},
        @{S="logs\studio.stderr.log";D="studio.stderr.tail.log";T=1000}, @{S="logs\audio-cpp.stdout.log";D="audio-cpp.stdout.tail.log";T=1000},
        @{S="logs\audio-cpp.stderr.log";D="audio-cpp.stderr.tail.log";T=1000}
    ) | ForEach-Object { Copy-Text (Join-Path $Root $_.S) (Join-Path $DiagnosticsOut $_.D) $_.T }

    $prompt = @"
irodori voice studioが正常に動作しないため、添付した診断ZIPを調査してください。

このPCでの実際のインストール先は diagnostics/environment.txt に記載されています。
配置例は D:\AI\irodori-voice-studio です。実際の絶対パス、既存モデル、runtime、audio.cpp、Python、GPU環境を可能な限り再利用してください。
新品環境として作り直したり、ディレクトリやドライブを勝手に変更したりしないでください。

大容量モデル、WAV、生成音声、実行バイナリ本体は同梱していません。diagnostics/file-inventory.csvの実パス、容量、存在状況を基に判断してください。
まず、導入済みで再利用できるもの、不足しているもの、壊れた設定、起動できない原因、自動取得可能なもの、手動導入が必要なもの、修正対象を整理してください。

可能であれば修正済みファイルをZIPで返してください。返却ZIPはirodori voice studioのルートへ上書きできる相対構造にしてください。
例えば D:\AI\irodori-voice-studio\scripts\start.ps1 は、ZIP内では scripts\start.ps1 としてください。
モデル、WAV、runtimeなどの大容量ファイルは返却ZIPへ含めず、既存ファイルを再利用してください。
バックアップが必要なファイルを明記し、削除が必要なら自動削除せず理由と対象を説明してください。

setup.bat、start.bat、stop.bat、UI起動、audio.cpp接続、モデル読み込み、短い音声生成まで確認できる修正を作ってください。
GPUは特定の製品名だけで判断せず、このPCで実際に利用可能なバックエンドを基準にし、CPUフォールバックも残してください。

公式リポジトリ：$RepositoryUrl
"@.Trim()
    Write-Utf8 (Join-Path $Work "AIに貼り付ける文章.txt") $prompt
    Write-Utf8 $PromptFile $prompt
    Write-Utf8 (Join-Path $SupportRoot "はじめに読んでください.txt") "診断ZIPをAIへ添付し、貼り付け文を送信してください。診断ZIPには実際のパス、PC名、ユーザー名、設定、ログが含まれます。送信前に内容を確認してください。秘密値は伏せ字にしています。"

    Compress-Archive -Path (Join-Path $Work "*") -DestinationPath $Bundle -CompressionLevel Optimal
    try { Set-Clipboard -Value $prompt } catch { }
    Write-Host "AI修理用ファイルを作成しました。" -ForegroundColor Green
    Write-Host "診断ZIP: $Bundle"
    Write-Host "貼り付け文: $PromptFile"
    Write-Host "依頼文はクリップボードにもコピーしました。"
    if (-not $NoOpen) { Start-Process explorer.exe -ArgumentList @("/select,`"$Bundle`"") }
} finally {
    if (Test-Path $Work) { Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue }
}



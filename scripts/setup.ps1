param(
    [switch]$NonInteractive,
    [switch]$AcceptLicenses,
    [switch]$UseExistingBackend,
    [switch]$ForceManagedBackend,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Data = Join-Path $Root "data"
$Runtime = Join-Path $Root "runtime"
$StatePath = Join-Path $Data "setup-state.json"
$ManualGuide = Join-Path $Root "download_links.html"
$ResultPage = Join-Path $Data "setup-result.html"

$AudioRelease = "release-0.3-qwen3-tts"
$AudioBase = "https://github.com/0xShug0/audio.cpp/releases/download/$AudioRelease"
$CpuPackage = @{
    Name = "audiocpp-windows-cpu-balance-e12fc74.zip"
    Url = "$AudioBase/audiocpp-windows-cpu-balance-e12fc74.zip"
    Sha256 = "cd863ce62604d8f4ac4b82681922ac3ce92bad1870144ada7d9ee5ff8d8a4c54"
}
$CudaPackage = @{
    Name = "audiocpp-windows-cuda-balance-e12fc74.zip"
    Url = "$AudioBase/audiocpp-windows-cuda-balance-e12fc74.zip"
    Sha256 = "08e70e80c9a8848e64d57b3d562d5ee274a09b12493766e37b6ca1bae331bbba"
}
$CudaRuntime = @{
    Name = "audiocpp-windows-cuda-runtime.zip"
    Url = "$AudioBase/audiocpp-windows-cuda-runtime.zip"
    Sha256 = "a3f2ecd5473fa198d7fefe1b4d7c86be33b5eac9d6fec8e0674dbefdd819a379"
}
$ModelRevision = "a70a4ecb70ee38ef43a5aeae9fbefb512778e262"
$ModelFiles = @(
    @{
        Name = "irodori-tts-500m-v3-q8_0.gguf"
        Url = "https://huggingface.co/audio-cpp/audio.cpp-gguf/resolve/$ModelRevision/Irodori-TTS-500M-v3-GGUF/irodori-tts-500m-v3-q8_0.gguf"
        Sha256 = "6f1d8d84f92207d4da674c9f6d350c692b7fde7c1fbaff4e7067fe7023b4b0c4"
        Size = 1093732096
    }
)

function Test-AudioBackend {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:8091/health" -TimeoutSec 2
        return $null -ne $response
    } catch { return $false }
}

function Get-GpuInfo {
    $result = [ordered]@{ Name = "Unknown"; Driver = ""; Compute = ""; Nvidia = $false }
    $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($smi) {
        try {
            $line = & $smi.Source --query-gpu=name,driver_version,compute_cap --format=csv,noheader 2>$null | Select-Object -First 1
            if ($line) {
                $parts = $line -split ",\s*"
                $result.Name = $parts[0]
                $result.Driver = $parts[1]
                if ($parts.Count -gt 2) { $result.Compute = $parts[2] }
                $result.Nvidia = $true
                return [pscustomobject]$result
            }
        } catch { }
    }
    try {
        $video = Get-CimInstance Win32_VideoController | Select-Object -First 1
        if ($video) {
            $result.Name = $video.Name
            $result.Nvidia = $video.Name -match "NVIDIA"
        }
    } catch { }
    return [pscustomobject]$result
}

function Confirm-Choice([string]$Prompt, [bool]$DefaultYes = $true) {
    if ($NonInteractive) { return $DefaultYes }
    $suffix = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$Prompt $suffix"
    if (-not $answer) { return $DefaultYes }
    return $answer -match "^[Yy]"
}

function Save-Json($Path, $Value) {
    $folder = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $temp = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -Path $temp -Encoding UTF8
    Move-Item -Force -Path $temp -Destination $Path
}

function Download-Verified($Item, $Destination) {
    if (Test-Path $Destination) {
        $existing = (Get-FileHash -Algorithm SHA256 -Path $Destination).Hash.ToLowerInvariant()
        if ($existing -eq $Item.Sha256) {
            Write-Host "Already verified: $($Item.Name)"
            return
        }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    $partial = "$Destination.partial"
    Write-Host "Downloading $($Item.Name)"
    Write-Host "Source: $($Item.Url)"
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source --location --fail --retry 5 --retry-delay 2 --continue-at - --output $partial $Item.Url
        if ($LASTEXITCODE -ne 0) { throw "Download failed for $($Item.Name)." }
    } else {
        Invoke-WebRequest -Uri $Item.Url -OutFile $partial -UseBasicParsing
    }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $partial).Hash.ToLowerInvariant()
    if ($actual -ne $Item.Sha256) {
        Remove-Item -Force $partial -ErrorAction SilentlyContinue
        throw "SHA-256 verification failed for $($Item.Name)."
    }
    Move-Item -Force $partial $Destination
}

function Ensure-Python {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    $pythonPath = if ($python) { $python.Source } else { $null }
    if (-not $python) {
        $localPython = Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"
        if (Test-Path $localPython) { $pythonPath = $localPython }
    }
    if ($pythonPath) {
        try {
            & $pythonPath --version | Out-Host
            if ($LASTEXITCODE -eq 0) { return $true }
        } catch { }
    }
    Write-Host "Python 3 is not installed."
    Write-Host "Automatic installation uses the official winget Python.Python.3.12 package and changes this Windows user environment."
    if (-not (Confirm-Choice "Install Python 3.12 for the current user?" $false)) { return $false }
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "winget is unavailable. See download_links.html."
        return $false
    }
    & $winget.Source install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { return $false }
    $installed = Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"
    return (Test-Path $installed)
}

function Write-LocalSettings([string]$BackendStart) {
    $path = Join-Path $Root "settings.local.json"
    $value = @{}
    if (Test-Path $path) {
        try {
            $parsed = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($property in $parsed.PSObject.Properties) { $value[$property.Name] = $property.Value }
        } catch { }
    }
    $value["backend_start_command"] = $BackendStart
    Save-Json $path $value
}

function Get-RunningBackendKind {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8091/health" -TimeoutSec 2
        return [string]$health.backend
    } catch { return "unknown" }
}

function Test-LlmReady {
    $healthUrl = "http://127.0.0.1:11438/health"
    $settingsPath = Join-Path $Root "settings.local.json"
    if (Test-Path $settingsPath) {
        try {
            $localSettings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([string]$localSettings.llm_health_url) { $healthUrl = [string]$localSettings.llm_health_url }
        } catch { }
    }
    try {
        $null = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 1
        return $true
    } catch { return $false }
}

function Test-ReferenceVoiceConfigured {
    $settingsPath = Join-Path $Root "settings.local.json"
    if (Test-Path $settingsPath) {
        try {
            $localSettings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([string]$localSettings.default_voice) { return $true }
        } catch { }
    }
    foreach ($folderName in @("voice_refs", "voices")) {
        $voiceFolder = Join-Path $Data $folderName
        if ((Test-Path $voiceFolder) -and ($null -ne (Get-ChildItem $voiceFolder -Filter *.wav -File -ErrorAction SilentlyContinue | Select-Object -First 1))) { return $true }
    }
    return $false
}

function Write-SetupResult([string]$BackendKind) {
    $required = @("Python 3", "Irodori-TTSモデル", "audio.cpp音声生成バックエンド")
    $recommended = @()
    if ($gpu.Nvidia -and $BackendKind -ne "cuda") {
        $recommended += "GPU高速化（現在は$BackendKind バックエンドで動作）"
    }
    $optional = @()
    if (-not (Test-ReferenceVoiceConfigured)) { $optional += "参照音声WAV（なくても音声生成は可能）" }
    if (-not (Test-LlmReady)) { $optional += "会話用LLM（会話機能を使う場合のみ必要）" }
    $remainingCount = $recommended.Count + $optional.Count
    $remainingSummary = if ($remainingCount -eq 0) {
        "未達成・未設定項目はありません。"
    } else {
        "未達成・未設定項目が合計${remainingCount}件あります。必須機能の動作可否とは分けて確認してください。"
    }

    $os = [Environment]::OSVersion.VersionString
    $driverLine = if ($gpu.Driver) { "`nNVIDIAドライバー: $($gpu.Driver)" } else { "" }
    $computeLine = if ($gpu.Compute) { "`nCompute Capability: $($gpu.Compute)" } else { "" }
    $recommendedText = if ($recommended.Count) { ($recommended | ForEach-Object { "- $_" }) -join "`n" } else { "- なし" }
    $optionalText = if ($optional.Count) { ($optional | ForEach-Object { "- $_" }) -join "`n" } else { "- なし" }
    $prompt = @"
Windows初心者向けに、irodori voice studioの導入手順を説明してください。
専門用語をできるだけ避け、操作を一つずつ順番に案内してください。
安全性とライセンスを確認できないファイルのダウンロードは勧めないでください。

【現在の環境】
OS: $os
GPU: $($gpu.Name)$driverLine$computeLine
音声バックエンド: $BackendKind

【導入済み】
$($required | ForEach-Object { "- $_" } | Out-String)
【未達成・推奨】
$recommendedText

【任意・未設定】
$optionalText

【依頼】
上記について、何が必須で何が任意かを最初に説明してください。
その後、このPCで安全に実行できる手順を初心者向けに一手順ずつ説明してください。
非公式バイナリを紹介する場合は、実行前に配布元、ライセンス、危険性を説明してください。
"@.Trim()

    function Encode([string]$Value) { return [Security.SecurityElement]::Escape($Value) }
    function To-List($Items, [string]$EmptyText) {
        if (-not $Items.Count) { return "<li class='none'>$(Encode $EmptyText)</li>" }
        return (($Items | ForEach-Object { "<li>$(Encode $_)</li>" }) -join "`n")
    }
    $requiredHtml = To-List $required "なし"
    $recommendedHtml = To-List $recommended "未達成項目はありません"
    $optionalHtml = To-List $optional "未設定項目はありません"
    $promptHtml = Encode $prompt
    New-Item -ItemType Directory -Force -Path $Data | Out-Null
    $html = @"
<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>セットアップ結果 - irodori voice studio</title><style>
body{margin:0;background:#f4f6f8;color:#20242a;font-family:"Yu Gothic UI","Meiryo",sans-serif}main{max-width:780px;margin:36px auto;padding:0 20px 50px}section{background:#fff;margin:16px 0;padding:20px 24px;border-radius:12px;box-shadow:0 3px 14px #18202b12}h1{font-size:26px}h2{font-size:19px}.ok{color:#18733c}.warn{color:#955800}.none{color:#607080}li{margin:9px 0;line-height:1.6}button,a.link{display:inline-block;border:0;border-radius:8px;padding:11px 16px;background:#2869d8;color:#fff;text-decoration:none;font-size:15px;cursor:pointer}a.link{background:#59636e;margin-left:8px}textarea{position:fixed;left:-10000px}#message{margin-left:10px;color:#18733c}
</style></head><body><main><h1>セットアップ結果</h1><p>音声生成に必要な準備は完了しました。次回からは <strong>start.bat</strong> を使用してください。</p><p><strong>$(Encode $remainingSummary)</strong></p>
<section><h2 class="ok">導入済み</h2><ul>$requiredHtml</ul><p>音声バックエンド: <strong>$(Encode $BackendKind)</strong></p></section>
<section><h2 class="warn">未達成・推奨</h2><ul>$recommendedHtml</ul></section>
<section><h2>任意・未設定</h2><ul>$optionalHtml</ul><p>任意項目は、使わない場合は設定しなくても問題ありません。</p></section>
<section><h2>分からない場合</h2><p>下のボタンで、ChatGPT・Geminiなどへそのまま貼り付けられる質問文をコピーできます。個人名、ファイル名、絶対パスは含みません。</p>
<textarea id="aiPrompt" readonly>$promptHtml</textarea><button onclick="copyPrompt()">未達成項目をAI相談用にコピー</button><a class="link" href="../download_links.html">手動ダウンロード案内</a><span id="message"></span></section>
<script>async function copyPrompt(){const t=document.getElementById('aiPrompt'),m=document.getElementById('message');try{await navigator.clipboard.writeText(t.value)}catch(e){t.style.position='static';t.select();document.execCommand('copy');t.style.position='fixed'}m.textContent='コピーしました'}</script>
</main></body></html>
"@
    Set-Content -Path $ResultPage -Value $html -Encoding UTF8
    Write-Host "Setup result: $ResultPage"
    Write-Host $remainingSummary
    Write-Host "Recommended items remaining: $($recommended.Count); optional items unset: $($optional.Count)"
    if (-not $NonInteractive) { Start-Process $ResultPage }
}

Write-Host ""
Write-Host "irodori voice studio setup"
Write-Host "==========================="
$gpu = Get-GpuInfo
$pythonReady = ($null -ne (Get-Command python.exe -ErrorAction SilentlyContinue)) -or (Test-Path (Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"))
$backendReady = Test-AudioBackend
if ($ForceManagedBackend) { $backendReady = $false }
Write-Host "OS: $([Environment]::OSVersion.VersionString)"
Write-Host "GPU: $($gpu.Name)"
if ($gpu.Driver) { Write-Host "NVIDIA driver: $($gpu.Driver)" }
if ($gpu.Compute) { Write-Host "Compute capability: $($gpu.Compute)" }
Write-Host "Python: $(if ($pythonReady) {'found'} else {'missing'})"
Write-Host "audio.cpp on port 8091: $(if ($backendReady) {'ready'} else {'missing'})"
Write-Host "The diagnostic information above stays on this PC."

if ($CheckOnly) { exit 0 }

Write-Host ""
Write-Host "Licenses and usage conditions"
Write-Host "- irodori voice studio: MIT"
Write-Host "- audio.cpp: Apache-2.0 (https://github.com/0xShug0/audio.cpp)"
Write-Host "- Irodori-TTS / model: MIT (https://huggingface.co/Aratako/Irodori-TTS-500M-v3)"
Write-Host "- audio.cpp standalone GGUF: MIT model contents (https://huggingface.co/audio-cpp/audio.cpp-gguf)"
Write-Host "Use only reference audio you have the right to use. Do not use voice cloning for impersonation, fraud, or misinformation."
Write-Host "Full notices are in LICENSES.md. Manual download links are in download_links.html."
if (-not $AcceptLicenses -and -not (Confirm-Choice "I have reviewed and accept these licenses and conditions." $false)) {
    Write-Host "Setup cancelled without downloading files."
    exit 2
}

if (-not (Ensure-Python)) {
    Write-Host "Python remains missing. See download_links.html, then run setup.bat again."
    if (-not $NonInteractive) { Start-Process $ManualGuide }
    exit 3
}

if ($UseExistingBackend -and -not $backendReady) {
    throw "UseExistingBackend was requested, but no healthy audio.cpp server is running on port 8091."
}
if ($backendReady) {
    Write-Host "Using the existing audio.cpp backend on port 8091."
    $state = [ordered]@{
        setup_version = 1; completed = $true; backend = "external"; gpu = $gpu.Name
        model = "external"; completed_at = (Get-Date).ToString("o")
    }
    Save-Json $StatePath $state
    Write-LocalSettings ""
    Write-SetupResult (Get-RunningBackendKind)
    Write-Host "Setup completed. Use start.bat from now on."
    exit 0
}

$compute = 0.0
if ($gpu.Compute) { [double]::TryParse($gpu.Compute, [Globalization.NumberStyles]::Any, [Globalization.CultureInfo]::InvariantCulture, [ref]$compute) | Out-Null }
$driverMajor = 0
if ($gpu.Driver) { [int]::TryParse(($gpu.Driver -split '\.')[0], [ref]$driverMajor) | Out-Null }
$cudaSupported = $gpu.Nvidia -and $compute -ge 7.5 -and $driverMajor -ge 580
$pascal = $gpu.Nvidia -and (($compute -gt 0 -and $compute -lt 7.5) -or $gpu.Name -match "GTX\s*10")
$backend = if ($cudaSupported) { "cuda" } else { "cpu" }

if ($pascal) {
    Write-Host "The official automatic CUDA package does not support GTX 10-series Pascal GPUs."
    Write-Host "A Pascal-compatible CUDA build must be prepared manually. The automatic fallback is CPU inference."
    if (-not (Confirm-Choice "Install the slower official CPU backend as a fallback?" $true)) {
        if (-not $NonInteractive) { Start-Process $ManualGuide }
        exit 4
    }
}
if ($gpu.Nvidia -and -not $cudaSupported -and -not $pascal) {
    Write-Host "The official CUDA package requires compute capability 7.5+ and NVIDIA driver 580+. CPU fallback will be installed."
}

$downloads = Join-Path $Runtime "downloads"
$audioDir = Join-Path $Runtime "audio_cpp"
$modelDir = Join-Path $Root "models\irodori-tts-GGUF"
Remove-Item -Force $StatePath -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $downloads, $audioDir, $modelDir | Out-Null

$packages = if ($backend -eq "cuda") { @($CudaRuntime, $CudaPackage) } else { @($CpuPackage) }
foreach ($package in $packages) {
    $zip = Join-Path $downloads $package.Name
    Download-Verified $package $zip
    Expand-Archive -Force -Path $zip -DestinationPath $audioDir
}
foreach ($modelFile in $ModelFiles) {
    Download-Verified $modelFile (Join-Path $modelDir $modelFile.Name)
}
$licenseDir = Join-Path $Runtime "licenses"
New-Item -ItemType Directory -Force -Path $licenseDir | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/0xShug0/audio.cpp/e12fc743ccb294753dcdced0be7778c76a178f95/LICENSE" -OutFile (Join-Path $licenseDir "audio.cpp-Apache-2.0.txt")
Invoke-WebRequest -UseBasicParsing -Uri "https://huggingface.co/Aratako/Irodori-TTS-500M-v3/raw/236c1e5/README.md" -OutFile (Join-Path $licenseDir "Irodori-TTS-model-card.md")
Copy-Item -Force (Join-Path $Root "LICENSE") (Join-Path $licenseDir "irodori-voice-studio-MIT.txt")
Copy-Item -Force (Join-Path $Root "LICENSES.md") (Join-Path $licenseDir "THIRD_PARTY_NOTICES.md")

$serverConfig = [ordered]@{
    host = "127.0.0.1"; port = 8091; backend = $backend; device = 0; threads = 4; lazy_load = $false
    models = @([ordered]@{
        id = "irodori-500m-v3-q8"; family = "irodori_tts"
        path = (Join-Path $modelDir "irodori-tts-500m-v3-q8_0.gguf").Replace("\", "/")
        task = "tts"; mode = "offline"
        session_options = [ordered]@{"irodori_tts.mem_saver"="true"; "irodori_tts.reference_cache_slots"="1"}
    })
}
Save-Json (Join-Path $Runtime "server.json") $serverConfig
Write-LocalSettings (Join-Path $PSScriptRoot "start_backend.ps1")
$backendStarter = Join-Path $PSScriptRoot "start_backend.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $backendStarter
if ($LASTEXITCODE -ne 0 -or -not (Test-AudioBackend)) {
    throw "The downloaded audio.cpp backend did not pass its startup check. Setup was not marked complete."
}
$state = [ordered]@{
    setup_version = 1; completed = $true; backend = $backend; gpu = $gpu.Name
    audio_release = $AudioRelease; model_revision = $ModelRevision; completed_at = (Get-Date).ToString("o")
}
Save-Json $StatePath $state
Write-SetupResult $backend
Write-Host "Setup completed. Use start.bat from now on."

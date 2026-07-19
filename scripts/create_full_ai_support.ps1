param([switch]$NoOpen,[string]$IssueText="")
$ErrorActionPreference="Stop";$Root=Split-Path -Parent $PSScriptRoot;$Out=Join-Path $Root "ai-support";$Base=Join-Path $PSScriptRoot "create_ai_support.ps1";$IssueFile=Join-Path $Out "困っている内容を記入してください.txt";$Stamp=Get-Date -Format "yyyyMMdd-HHmmss";$Work=Join-Path ([IO.Path]::GetTempPath()) "irodori-full-$Stamp-$PID";$Zip=Join-Path $Out "真・AI調査用一式-$Stamp.zip";$Txt=Join-Path $Out "真・AIへ貼り付ける文章-$Stamp.txt"
function W([string]$p,[string]$t){New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p)|Out-Null;Set-Content -LiteralPath $p -Value $t -Encoding UTF8}
function Template {W $IssueFile @"
【重要】
困っている内容を書いた後、必ず「上書き保存」してください。
Ctrl + Sで保存してからメモ帳を閉じてください。
保存しないまま閉じると、症状がAIへ伝わりません。
中止する場合は記入欄へ「中止」と書いて保存してください。

【記入欄：ここから下へ書いてください】


【記入欄ここまで】

【記入例・この部分は変更不要です】
困っていること：start.batを押しても画面が開きません。
最後の操作：setup.bat完了後、start.batを押しました。
表示：ターミナルが一瞬だけ表示されました。
希望：ブラウザーにUIが表示されてほしいです。
"@}
function ReadIssue {$t=Get-Content $IssueFile -Raw -Encoding UTF8;$a="【記入欄：ここから下へ書いてください】";$b="【記入欄ここまで】";$i=$t.IndexOf($a);$j=$t.IndexOf($b);if($i -lt 0 -or $j -le $i){return ""};$t.Substring($i+$a.Length,$j-$i-$a.Length).Trim()}
function Check([string]$n,[scriptblock]$a){$nl=[Environment]::NewLine;try{$global:LASTEXITCODE=0;$o=(& $a 2>&1|Out-String).Trim();"[$n]"+$nl+"終了コード: $LASTEXITCODE"+$nl+$o+$nl}catch{"[$n]"+$nl+"実行失敗: $($_.Exception.Message)"+$nl}}
New-Item -ItemType Directory -Force -Path $Out|Out-Null
if([string]::IsNullOrWhiteSpace($IssueText)){while($true){Template;Write-Host "メモ帳へ症状を書き、必ず Ctrl + S で保存して閉じてください。" -ForegroundColor Yellow;$p=Start-Process notepad.exe -ArgumentList @($IssueFile) -PassThru;$p.WaitForExit();$IssueText=ReadIssue;if($IssueText -eq "中止"){exit 2};if($IssueText.Length -ge 10){break};Write-Host "記入・保存を確認できません。もう一度開きます。" -ForegroundColor Red}}else{$IssueText=$IssueText.Trim();if($IssueText.Length -lt 10){throw "症状は10文字以上必要です。"}}
if(-not(Test-Path $Base)){throw "基本診断スクリプトがありません。"}
try{
 New-Item -ItemType Directory -Force -Path $Work|Out-Null;& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Base -NoOpen;if($LASTEXITCODE){throw "基本診断ZIP作成失敗"}
 $z=Get-ChildItem $Out -Filter "AIに渡す診断情報-*.zip"|Sort-Object LastWriteTime -Descending|Select-Object -First 1
 Expand-Archive $z.FullName $Work -Force
 $extraNames=@("LICENSE",".gitignore",".gitattributes")
 $extraExtensions=@(".jpg",".jpeg",".png",".svg",".yml",".yaml",".toml")
 Get-ChildItem $Root -Recurse -File -Force|Where-Object{
  $rel=$_.FullName.Substring($Root.Length).TrimStart("\")
  $parts=$rel -split "[\\/]"
  -not($parts|Where-Object{$_ -in @(".git",".venv","ai-support","data","outputs","logs","models","runtime","__pycache__")}) -and
  $_.Length -le 5MB -and ($_.Name -in $extraNames -or $_.Extension.ToLowerInvariant() -in $extraExtensions)
 }|ForEach-Object{
  $rel=$_.FullName.Substring($Root.Length).TrimStart("\")
  $dest=Join-Path (Join-Path $Work "source") $rel
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest)|Out-Null
  Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
 }
 W (Join-Path $Work "diagnostics\user-issue.txt") $IssueText
 $c=@();$c+=Check "PowerShell構文検査" {$bad=@();Get-ChildItem (Join-Path $Root "scripts") -Filter *.ps1|ForEach-Object{$e=$null;[Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$null,[ref]$e)|Out-Null;if($e.Count){$bad+="$($_.Name): $e"}};if($bad){$bad;$global:LASTEXITCODE=1}else{"OK"}}
 if(Get-Command python.exe -ErrorAction SilentlyContinue){$c+=Check "Python単体テスト" {Push-Location $Root;try{python.exe -m unittest discover -s tests -v}finally{Pop-Location}};$c+=Check "公開安全チェック" {Push-Location $Root;try{python.exe scripts\public_safety_check.py}finally{Pop-Location}}}else{$c+="[Python検査] Pythonがないため未実行"}
 if(Get-Command node.exe -ErrorAction SilentlyContinue){$c+=Check "JavaScript構文検査" {node.exe --check (Join-Path $Root "web\app.js")}}else{$c+="[JavaScript検査] Node.jsがないため未実行（必須ではありません）"}
 W (Join-Path $Work "diagnostics\automated-checks.txt") ($c -join [Environment]::NewLine)
 $prompt=@"
irodori voice studioで解決できない問題が発生しています。
このTXTの文章をチャットへ貼り付け、一緒に添付した「真・AI調査用一式」ZIPを解析してください。

【利用者が困っていること】
$IssueText

【指示】
1. 利用者の症状を最優先してください。
2. diagnosticsのenvironment.txt、file-inventory.csv、user-issue.txt、automated-checks.txt、設定、ログ、source内コードを突き合わせてください。
3. 導入済み、不足、壊れた設定、起動不能原因を分けてください。
4. プロジェクト全体をゼロベースでバグチェックし、主原因と副次的な不具合を分けてください。
5. CSV内の実際の絶対パス、モデル、runtime、audio.cpp、Python、GPU環境を可能な限り再利用してください。
6. ディレクトリやドライブを勝手に変えず、現在の環境へ上書きして動く修正にしてください。
7. 情報不足なら推測で大きく変更せず、先に最大3問まで質問してください。
8. 削除や初期化が必要なら、理由、対象、バックアップ方法を説明してください。

可能なら修正済みファイルをZIPで返してください。返却ZIPはソフトのルートへそのまま上書きできる相対構造にしてください。
モデル、WAV、生成音声、runtime本体は含めず既存物を再利用してください。
修正後はsetup.bat、start.bat、stop.bat、UI、audio.cpp接続、モデル読込、短い音声生成まで確認し、初心者向けに説明してください。
公式リポジトリ：https://github.com/rucks1989-blip/irodori-voice-studio
"@.Trim()
 W (Join-Path $Work "AIへ貼り付ける文章.txt") $prompt;W $Txt $prompt;Compress-Archive -Path (Join-Path $Work "*") -DestinationPath $Zip -CompressionLevel Optimal;try{Set-Clipboard $prompt}catch{};Write-Host "真・AI調査用ファイルを作成しました。" -ForegroundColor Green;Write-Host "TXTをチャットへ貼り、ZIPも添付してください。";Write-Host "ZIP: $Zip";Write-Host "TXT: $Txt";if(-not $NoOpen){Start-Process explorer.exe -ArgumentList $Out}
}finally{if(Test-Path $Work){Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue}}


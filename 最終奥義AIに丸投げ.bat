@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo irodori voice studio - AI修理用ファイル作成
echo =============================================
echo.
echo 現在の実ディレクトリ、PC環境、設定、ログを含む診断ZIPを作成します。
echo モデル、WAV、生成音声、実行バイナリ本体はZIPへ含めません。
echo APIキー、トークン、パスワードは伏せ字にします。
echo 作成したZIPを外部AIへ送る前に、内容を確認してください。
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_ai_support.ps1" %*
if errorlevel 1 (
  echo.
  echo AI修理用ファイルの作成に失敗しました。上のメッセージを確認してください。
  pause
  exit /b 1
)

echo.
echo 作成が完了しました。ai-supportフォルダーを確認してください。
pause


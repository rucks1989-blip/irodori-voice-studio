@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo irodori voice studio - 真・AIバグ調査用ファイル作成
echo.
echo メモ帳へ困っている内容を書き、必ず Ctrl + S で保存してください。
echo 最後に作られるTXTをチャットへ貼り、ZIPも添付してください。
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_full_ai_support.ps1" %*
if errorlevel 1 (
 echo 作成を完了できませんでした。上のメッセージを確認してください。
 pause
 exit /b 1
)
echo 作成完了。ai-supportフォルダーを確認してください。
pause


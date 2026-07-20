@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
if errorlevel 1 (
  echo.
  echo 起動に失敗しました。上の日本語メッセージを確認してください。
  pause
)

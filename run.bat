@echo off
chcp 65001 >nul
title 大云壁画工具箱 · 精卫 Jingwei
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
echo.
pause

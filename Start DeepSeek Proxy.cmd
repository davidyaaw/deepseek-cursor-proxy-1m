@echo off
rem Double-clickable wrapper for start-deepseek-proxy.ps1.
rem Opens a console window that shows the Cursor Base URL and keeps the proxy alive.
title DeepSeek Proxy for Cursor
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-deepseek-proxy.ps1" %*

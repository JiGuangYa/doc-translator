@echo off
chcp 65001 >nul
REM doc-translator management script shim: bypasses execution policy to call manage.ps1
REM Usage: manage.bat {install|start|stop|restart|status|update}
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage.ps1" %*

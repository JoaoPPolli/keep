@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Feche a janela do Keep antes de atualizar.
"runtime\python.exe" -X utf8 bootstrap.py --update
pause

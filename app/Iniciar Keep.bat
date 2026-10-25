@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Keep 1.7.0
if not exist "runtime\python.exe" (
  echo Extraia todo o arquivo ZIP antes de abrir o Keep.
  pause
  exit /b 1
)
"runtime\python.exe" -X utf8 bootstrap.py
if errorlevel 1 goto error
echo.
echo Keep 1.7.0 em portugues brasileiro.
echo Deixe esta janela aberta durante os downloads.
"runtime\python.exe" -X utf8 server.py --open
if errorlevel 1 goto error
exit /b
:error
echo.
echo Nao foi possivel iniciar. Confira a mensagem acima.
echo Se outra versao estiver aberta, feche-a e tente novamente.
pause

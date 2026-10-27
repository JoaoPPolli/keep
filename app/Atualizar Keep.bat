@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Atualizar Keep para 2.2.1
if not exist "update_keep.py" (
  echo Extraia todo o ZIP de atualizacao antes de abrir este arquivo.
  pause
  exit /b 1
)
set "KEEP_UPDATE_PACKAGE=%~dp0"
powershell.exe -NoProfile -STA -Command "$ErrorActionPreference='Stop'; try { Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Selecione a pasta do Keep que voce ja usa (onde esta Iniciar Keep.bat)'; $d.ShowNewFolderButton=$false; if ($d.ShowDialog() -ne 'OK') { Write-Host 'Atualizacao cancelada.'; exit 0 }; $py=Join-Path $d.SelectedPath 'runtime\python.exe'; if (-not (Test-Path -LiteralPath $py -PathType Leaf)) { throw 'Esta pasta nao tem o Keep completo. Escolha a pasta que contem Iniciar Keep.bat e runtime.' }; & $py -X utf8 (Join-Path $env:KEEP_UPDATE_PACKAGE 'update_keep.py') --target $d.SelectedPath; exit $LASTEXITCODE } catch { Write-Host ('Nao foi possivel atualizar: '+$_.Exception.Message); exit 1 }"
set "KEEP_UPDATE_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %KEEP_UPDATE_RESULT%

@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1 && goto start
)
if /i "%~1"=="stop" exit /b 0
py -3.13 -c "import struct; assert struct.calcsize('P') == 8" >nul 2>&1 && goto py313
py -3.12 -c "import struct; assert struct.calcsize('P') == 8" >nul 2>&1 && goto py312
if not exist ".local\python\tools\python.exe" (
    echo Downloading Python 3.13 for Windows x64...
    powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $d=Join-Path (Get-Location).Path '.local'; New-Item -ItemType Directory -Force -Path $d | Out-Null; $zip=Join-Path $d 'python-3.13.17.zip'; curl.exe -fLsS --retry 2 --max-time 120 'https://api.nuget.org/v3-flatcontainer/python/3.13.17/python.3.13.17.nupkg' -o $zip; if ($LASTEXITCODE -ne 0) { throw 'Python download failed.' }; $stream=[IO.File]::OpenRead($zip); try { $hash=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($stream)).Replace('-','') } finally { $stream.Dispose() }; if ($hash -ne 'DCE409744B088F94C6E37BA41F8100C384C27E029E843237364C602D30D475E5') { throw 'Python download checksum mismatch.' }; Add-Type -AssemblyName System.IO.Compression.FileSystem; $stage=Join-Path $d ([Guid]::NewGuid().ToString()); [IO.Compression.ZipFile]::ExtractToDirectory($zip,$stage); [IO.Directory]::Move($stage,(Join-Path $d 'python')); Remove-Item -LiteralPath $zip"
    if errorlevel 1 goto failed
)
".local\python\tools\python.exe" -m venv .venv
if errorlevel 1 goto failed
goto start

:py313
py -3.13 -m venv .venv || goto failed
goto start

:py312
py -3.12 -m venv .venv || goto failed

:start
".venv\Scripts\python.exe" local.py %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Startup failed. See the message above.
if /i "%~1"=="" pause
exit /b 1

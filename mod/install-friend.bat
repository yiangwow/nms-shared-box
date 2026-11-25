@echo off
setlocal
rem One-time setup for the NMS shared box. Run it from the unzipped folder.
set "HERE=%~dp0"
set "MODS=C:\NMSpyMods"
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

if exist "%PYEXE%" goto havepython
echo Installing Python 3.11 for this user...
winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
if not exist "%PYEXE%" (
  echo.
  echo Python did not install automatically.
  echo Install Python 3.11 from https://www.python.org/downloads/release/python-3119/
  echo then run this file again.
  pause
  exit /b 1
)
:havepython
"%PYEXE%" -m pip install --quiet --upgrade pip
"%PYEXE%" -m pip install --quiet nmspy websockets==13.1
if errorlevel 1 (
  echo Installing NMS.py failed. Send a screenshot of this window.
  pause
  exit /b 1
)

if not exist "%MODS%\_sharedbox\sharedbox_lib" mkdir "%MODS%\_sharedbox\sharedbox_lib"
copy /y "%HERE%shared_box.py" "%MODS%\" >nul
copy /y "%HERE%start-nms-shared.bat" "%MODS%\" >nul
copy /y "%HERE%_sharedbox\sharedbox_lib\*.py" "%MODS%\_sharedbox\sharedbox_lib\" >nul
if not exist "%MODS%\shared_box.toml" copy /y "%HERE%shared_box.toml" "%MODS%\" >nul

rem Tell NMS.py where the mods live so it doesn't ask.
if not exist "%APPDATA%\pymhf\nmspy" mkdir "%APPDATA%\pymhf\nmspy"
if not exist "%APPDATA%\pymhf\nmspy\pymhf.local.toml" (
  >"%APPDATA%\pymhf\nmspy\pymhf.local.toml" echo [pymhf.local_config]
  >>"%APPDATA%\pymhf\nmspy\pymhf.local.toml" echo mod_dir = "C:\\NMSpyMods"
)

echo.
echo Done.
echo 1. Set your name in C:\NMSpyMods\shared_box.toml
echo 2. In the game, empty your corvette cargo box 9 before the first time.
echo 3. Start the game with C:\NMSpyMods\start-nms-shared.bat
pause

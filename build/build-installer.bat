@echo off
setlocal
cd /d "%~dp0\.."

echo.
echo === CrimeScanner installer build ===
echo.

echo [1/4] Installing Node dependencies...
call npm install
if errorlevel 1 goto :fail

echo.
echo [2/4] Building Python backend...
call python -m pip install --upgrade pyinstaller
call npm run build:backend
if errorlevel 1 goto :fail

echo.
echo [3/4] Building renderer...
call npm run build:renderer
if errorlevel 1 goto :fail

echo.
echo [4/4] Building Windows installer...
call npx electron-builder --win nsis
if errorlevel 1 goto :fail

echo.
echo Done. Installer created in the dist-electron folder.
goto :eof

:fail
echo.
echo Build failed. Check the error above.
exit /b 1

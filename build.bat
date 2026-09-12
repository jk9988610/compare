@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [1/2] Installing dependencies...
python -m pip install -r requirements.txt -r requirements-dev.txt
if errorlevel 1 (
  echo.
  echo ERROR: pip install failed.
  pause
  exit /b 1
)

echo.
echo [2/2] Building with PyInstaller...
python -m PyInstaller --noconfirm compare.spec
if errorlevel 1 (
  echo.
  echo ERROR: PyInstaller failed. See messages above.
  pause
  exit /b 1
)

echo.
if exist "dist\目录对比\目录对比.exe" (
  echo Build OK:
  echo   %CD%\dist\目录对比\目录对比.exe
) else (
  echo Build finished, but exe not found under dist\
  dir /s /b dist 2>nul
  pause
  exit /b 1
)

echo.
pause
endlocal

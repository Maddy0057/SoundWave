@echo off
echo ==========================================
echo    SoundWave .exe Builder
echo ==========================================
echo.

REM Clean previous builds
if exist "dist" rd /s /q dist
if exist "build" rd /s /q build

echo [1/2] Building SoundWave.exe ...
echo      This may take 2-5 minutes...
echo.

pyinstaller SoundWave.spec --clean --noconfirm

echo.
if exist "dist\SoundWave.exe" (
    echo ==========================================
    echo    BUILD SUCCESSFUL!
    echo    Output: dist\SoundWave.exe
    echo ==========================================
    echo.
    echo NOTE: Copy 'yt-dlp.exe' to the same folder
    echo       as SoundWave.exe before running.
    echo.
) else (
    echo ==========================================
    echo    BUILD FAILED - Check errors above
    echo ==========================================
)
pause

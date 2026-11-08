@echo off
rem Build Riffle: light app (PyInstaller) -> installer (Inno Setup) -> visual pack zip.
rem Output lands in ..\dist. Run from anywhere.
setlocal
cd /d "%~dp0"

python pack_needs.py || goto :fail
python -m PyInstaller --noconfirm --clean --distpath ..\build\dist --workpath ..\build\work riffle.spec || goto :fail
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" /Q riffle.iss || goto :fail
python make_pack.py || goto :fail
python make_voice_pack.py || goto :fail

echo.
echo Done. Files in %~dp0..\dist:
dir /b ..\dist
exit /b 0

:fail
echo Build failed.
exit /b 1

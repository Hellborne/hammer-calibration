@echo off
rem Builds dist\HammerCalibration.exe: Python and all libraries go inside the exe.
rem Needs the standard Python 3.12 from python.org. Anaconda does not work here:
rem its exe would not start on other computers without Anaconda's DLLs.
cd /d "%~dp0"
if not exist .build-venv\Scripts\python.exe (
    py -3.12 -m venv .build-venv || goto :error
)
.build-venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller || goto :error
.build-venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name HammerCalibration ^
    --copy-metadata pyusb --copy-metadata bleak ^
    --distpath dist --workpath build --specpath build "hammer spindown.py" || goto :error
echo.
echo Done: dist\HammerCalibration.exe
pause
exit /b 0

:error
echo.
echo Build failed, see the messages above.
pause
exit /b 1

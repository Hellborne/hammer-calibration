@echo off
cd /d "%~dp0"
set "CONDA=%USERPROFILE%\anaconda3"
if exist "%CONDA%\Scripts\activate.bat" call "%CONDA%\Scripts\activate.bat" "%CONDA%"
python -c "import usb.core, libusb_package, bleak" >nul 2>&1
if errorlevel 1 (
    echo Installing pyusb, libusb-package and bleak...
    python -m pip install pyusb libusb-package bleak
    if errorlevel 1 echo Could not install some libraries, trying anyway...
)
python "hammer spindown.py"
if errorlevel 1 pause

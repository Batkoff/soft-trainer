@echo off
cd /d "%~dp0"
python scripts\prepare_demo.py
if errorlevel 1 exit /b 1
docker compose up --build -d
if errorlevel 1 exit /b 1
echo.
echo Open http://localhost:8080
echo Credentials: .demo-credentials.txt
pause

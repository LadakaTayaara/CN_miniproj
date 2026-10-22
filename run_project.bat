@echo off
title Dynamic Load Balancer & Health Monitor
color 0b

echo ============================================================
echo   Dynamic Layer-7 Load Balancer & Health Monitor Cluster
echo ============================================================
echo.

:: 1. Verify Python is installed and accessible
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not found in your system PATH!
    echo Please install Python 3.10+ and ensure "Add Python to PATH" is checked.
    echo.
    pause
    exit /b 1
)

:: 2. Install / verify dependencies
echo [*] Step 1/3: Checking and verifying dependencies from requirements.txt...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [WARNING] Dependency installation returned an error. Proceeding anyway...
)
echo.

:: 3. Inform user and prepare dashboard launch
echo [*] Step 2/3: Launching cluster processes (4 Backends + Load Balancer)...
echo [*] Reverse Proxy Route:    http://127.0.0.1:8080/
echo [*] Live Web Dashboard:    http://localhost:8080/dashboard
echo.
echo [*] Step 3/3: Opening Live Telemetry Dashboard in your browser...
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8080/dashboard"

:: 4. Start Cluster Orchestrator
python run_cluster.py

echo.
echo [*] Cluster orchestrator terminated.
pause

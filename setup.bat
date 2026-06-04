@echo off
REM setup.bat -- One-time setup for Store Intelligence on Windows.
REM Run this once after cloning the repo.

setlocal enabledelayedexpansion

echo ============================================================
echo  Store Intelligence -- Setup
echo ============================================================
echo.

REM ---- Check Docker ----
docker --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not installed or not in PATH.
    echo         Install Docker Desktop: https://www.docker.com/products/docker-desktop
    pause
    exit /b 1
)
echo [OK] Docker found.

docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker daemon is not running. Start Docker Desktop first.
    pause
    exit /b 1
)
echo [OK] Docker daemon running.

REM ---- Check Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [WARN] Python not found in PATH.
    echo        The API runs inside Docker without Python.
    echo        Install Python 3.10+ from https://www.python.org/downloads/
    set PYTHON_OK=0
) else (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
    echo [OK] Python !PY_VER! found.
    set PYTHON_OK=1
)

REM ---- Create directory structure ----
for %%d in (data data\Store_1 data\Store_2 events) do (
    if not exist "%%d" (
        mkdir "%%d"
        echo [OK] Created %%d\
    ) else (
        echo [OK] %%d\ already exists.
    )
)

REM ---- Check for footage ----
echo.
echo [INFO] Checking for video footage...

if exist "data\Store_1\CAM 3 - entry.mp4" (
    echo [OK] Store 1 footage found in data\Store_1\
) else (
    echo [WARN] Store 1 footage not found in data\Store_1\
    echo        Copy the Store 1 video files there before running run.bat.
    echo        Expected: "CAM 3 - entry.mp4"  "CAM 1 - zone.mp4"
    echo                  "CAM 2 - zone.mp4"   "CAM 5 - billing.mp4"
)

if exist "data\Store_2\entry 1.mp4" (
    echo [OK] Store 2 footage found in data\Store_2\
) else (
    echo [WARN] Store 2 footage not found in data\Store_2\
    echo        Copy the Store 2 video files there before running run.bat.
)

REM ---- Check sample data ----
echo.
if exist "data\sample_events.jsonl" (
    echo [OK] data\sample_events.jsonl found.
) else (
    echo [WARN] data\sample_events.jsonl not found.
)
if exist "data\POS_-_sample_transactions.csv" (
    echo [OK] data\POS_-_sample_transactions.csv found.
) else (
    echo [WARN] data\POS_-_sample_transactions.csv not found.
)

REM ---- Install Python dependencies ----
echo.
if "!PYTHON_OK!"=="1" (
    echo [INFO] Installing Python dependencies...
    pip install fastapi uvicorn aiosqlite pydantic httpx pytest pytest-asyncio anyio rich requests opencv-python-headless --quiet
    if errorlevel 1 (
        echo [WARN] Some pip installs failed. API will still run via Docker.
    ) else (
        echo [OK] Python dependencies installed.
    )

    echo [INFO] Attempting to install ultralytics (YOLO -- optional)...
    pip install ultralytics --quiet >nul 2>&1
    python -c "import ultralytics" >nul 2>&1
    if errorlevel 1 (
        echo [INFO] ultralytics not available -- HOG detector will be used instead.
    ) else (
        echo [OK] ultralytics installed -- full YOLO pipeline available.
    )
)

REM ---- Build Docker images ----
echo.
echo [INFO] Building Docker images (takes a few minutes on first run)...
docker compose build
if errorlevel 1 (
    echo [ERROR] docker compose build failed. Check output above.
    pause
    exit /b 1
)
echo [OK] Docker images built.

REM ---- Run tests ----
echo.
if "!PYTHON_OK!"=="1" (
    echo [INFO] Running test suite...
    python -m pytest tests\ -q --tb=short 2>&1
    if errorlevel 1 (
        echo [WARN] Some tests failed.
    ) else (
        echo [OK] All tests passed.
    )
)

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Place footage in:
echo    data\Store_1\  -- CAM 3 - entry.mp4, CAM 1 - zone.mp4,
echo                      CAM 2 - zone.mp4, CAM 5 - billing.mp4
echo    data\Store_2\  -- entry 1.mp4, zone.mp4, billing_area.mp4
echo.
echo  Then run:  run.bat
echo ============================================================
echo.
pause

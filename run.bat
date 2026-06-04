@echo off
REM run.bat -- Start Store Intelligence, process footage, ingest events.
REM
REM Usage:
REM   run.bat                          auto-process all stores in data\
REM   run.bat data\Store_1 ST1008      single store
REM   run.bat data\Store_2 ST1076      single store

setlocal enabledelayedexpansion

set STORE_DIR=%~1
set STORE_ID=%~2
set API_URL=http://localhost:8000

echo ============================================================
echo  Store Intelligence -- Run
echo  API: %API_URL%
echo ============================================================
echo.

REM ---- Check Docker ----
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running. Start Docker Desktop first.
    pause
    exit /b 1
)

REM ---- Step 1: Start containers ----
echo [1/5] Starting API and dashboard...
docker compose up -d
if errorlevel 1 (
    echo [ERROR] docker compose up failed.
    pause
    exit /b 1
)
echo [OK] Containers started.

REM ---- Step 2: Wait for API health ----
echo.
echo [2/5] Waiting for API to be healthy...
set /a ATTEMPTS=0
:WAIT_LOOP
set /a ATTEMPTS+=1
if !ATTEMPTS! gtr 40 (
    echo [ERROR] API not healthy after 40 attempts.
    echo         Run:  docker compose logs api
    pause
    exit /b 1
)
curl -sf %API_URL%/health >nul 2>&1
if errorlevel 1 (
    echo   Attempt !ATTEMPTS!/40 -- waiting...
    timeout /t 3 /nobreak >nul
    goto WAIT_LOOP
)
echo [OK] API is healthy at %API_URL%

REM ---- Step 3: Ingest sample data ----
echo.
echo [3/5] Ingesting sample data...

python --version >nul 2>&1
if errorlevel 1 (
    echo [SKIP] Python not found -- skipping sample data ingest.
    goto SKIP_SAMPLE
)

REM Write ingest helper to a temp file to avoid quote/variable expansion issues
set TMPPY=%TEMP%\si_ingest_sample.py
(
echo import json, requests, csv, sys, os
echo.
echo api = sys.argv[1]
echo.
echo # Ingest sample events
echo sample = "data/sample_events.jsonl"
echo if os.path.exists^(sample^):
echo     events = [json.loads^(l^) for l in open^(sample^) if l.strip^(^)]
echo     r = requests.post^(f"{api}/events/ingest", json={"events": events}, timeout=30^)
echo     d = r.json^(^)
echo     print^(f"  sample_events: accepted={d['accepted']} dup={d['duplicate']} rejected={d['rejected']}"^)
echo else:
echo     print^("  [SKIP] data/sample_events.jsonl not found"^)
echo.
echo # Ingest POS transactions
echo pos = "data/POS_-_sample_transactions.csv"
echo if os.path.exists^(pos^):
echo     import uuid
echo     rows = list^(csv.DictReader^(open^(pos^)^)^)
echo     txns = []
echo     for row in rows:
echo         try:
echo             txns.append^({
echo                 "order_id": str^(row.get^("order_id", uuid.uuid4^(^)^)^),
echo                 "store_id": row.get^("store_id", "ST1008"^),
echo                 "order_date": row.get^("order_date", "2026-04-10"^),
echo                 "order_time": row.get^("order_time", "20:00:00"^),
echo                 "product_id": row.get^("product_id", ""^),
echo                 "brand_name": row.get^("brand_name", ""^),
echo                 "total_amount": float^(row.get^("total_amount", row.get^("basket_value_inr", 0^)^)^),
echo             }^)
echo         except:
echo             pass
echo     if txns:
echo         r2 = requests.post^(f"{api}/events/pos/ingest", json=txns, timeout=30^)
echo         print^(f"  POS transactions: {r2.json^(^)}"^)
echo else:
echo     print^("  [SKIP] POS file not found"^)
) > "%TMPPY%"

python "%TMPPY%" %API_URL%
del "%TMPPY%" >nul 2>&1

:SKIP_SAMPLE

REM ---- Step 4: Detection pipeline ----
echo.
echo [4/5] Running detection pipeline...

python --version >nul 2>&1
if errorlevel 1 (
    echo [SKIP] Python not available -- skipping detection.
    goto SKIP_DETECTION
)

if not "%STORE_DIR%"=="" (
    REM Single store specified by user
    call :RUN_STORE "%STORE_DIR%" "%STORE_ID%"
) else (
    REM Auto-detect all stores
    if exist "data\Store_1" (
        echo   Found data\Store_1
        call :RUN_STORE_1
    )
    if exist "data\Store_2" (
        echo   Found data\Store_2
        call :RUN_STORE "data\Store_2" "ST1076"
    )
)

:SKIP_DETECTION

REM ---- Step 5: Open browser ----
echo.
echo [5/5] Opening API docs in browser...
timeout /t 2 /nobreak >nul
start "" "%API_URL%/docs"

echo.
echo ============================================================
echo  System is running!
echo.
echo  API Docs   : %API_URL%/docs
echo  Health     : %API_URL%/health
echo.
echo  Store 1 (ST1008):
echo    Metrics  : %API_URL%/stores/ST1008/metrics
echo    Funnel   : %API_URL%/stores/ST1008/funnel
echo    Heatmap  : %API_URL%/stores/ST1008/heatmap
echo    Anomalies: %API_URL%/stores/ST1008/anomalies
echo.
echo  Store 2 (ST1076):
echo    Metrics  : %API_URL%/stores/ST1076/metrics
echo    Funnel   : %API_URL%/stores/ST1076/funnel
echo    Heatmap  : %API_URL%/stores/ST1076/heatmap
echo    Anomalies: %API_URL%/stores/ST1076/anomalies
echo.
echo  Dashboard  : docker compose logs -f dashboard
echo  Stop all   : docker compose down
echo ============================================================
echo.
pause
goto :EOF


REM ==========================================================================
REM  Subroutine: Run Store 1 (uses HOG detector, no YOLO download needed)
REM ==========================================================================
:RUN_STORE_1
echo   Processing Store 1 (ST1008) with HOG detector...
if not exist "events" mkdir events

if not exist "data\Store_1\CAM 3 - entry.mp4" (
    echo   [SKIP] Store 1 footage not found in data\Store_1\
    echo          Copy CAM 3 - entry.mp4, CAM 1 - zone.mp4, etc. there first.
    goto :EOF
)

python run_store1.py --api %API_URL% --output events
goto :EOF


REM ==========================================================================
REM  Subroutine: Run a generic store (uses YOLO pipeline, falls back to HOG)
REM ==========================================================================
:RUN_STORE
set "S_DIR=%~1"
set "S_ID=%~2"
echo   Processing %S_DIR% as %S_ID%...
if not exist "events" mkdir events

REM Write the store processing script to a temp file
set TMPPY2=%TEMP%\si_process_store.py
(
echo import subprocess, sys, os, glob, json, requests
echo.
echo store_dir = sys.argv[1]
echo store_id  = sys.argv[2]
echo api_url   = sys.argv[3]
echo out_dir   = "events"
echo.
echo def run_cam^(video, cam_id, cam_type^):
echo     layout = os.path.join^(store_dir, "store_layout.json"^)
echo     cmd = [sys.executable, "-m", "pipeline.detect",
echo            "--video", video, "--store-id", store_id,
echo            "--camera-id", cam_id, "--camera-type", cam_type,
echo            "--output", f"{out_dir}/{store_id}_{cam_id}.jsonl",
echo            "--staff-colour", "black"]
echo     if os.path.exists^(layout^):
echo         cmd += ["--layout", layout]
echo     print^(f"  Detecting: {os.path.basename^(video^)} -> {cam_id}"^)
echo     r = subprocess.run^(cmd, capture_output=True, text=True^)
echo     if r.returncode != 0:
echo         print^(f"  [WARN] {r.stderr[-200:]}"^)
echo.
echo patterns = [
echo     ^(["*entry*", "*Entry*"], "entry",   "CAM_ENTRY"^),
echo     ^(["*zone*",  "*Zone*",  "*floor*"], "floor",   "CAM_FLOOR"^),
echo     ^(["*billing*","*Billing*"],          "billing", "CAM_BILLING"^),
echo ]
echo for pats, cam_type, prefix in patterns:
echo     idx = 1
echo     for pat in pats:
echo         for vf in sorted^(glob.glob^(os.path.join^(store_dir, pat + ".mp4"^)^)^):
echo             run_cam^(vf, f"{prefix}_0{idx}", cam_type^)
echo             idx += 1
echo.
echo all_ev = []
echo for fp in sorted^(glob.glob^(f"{out_dir}/{store_id}_CAM*.jsonl"^)^):
echo     with open^(fp^) as f:
echo         all_ev += [json.loads^(l^) for l in f if l.strip^(^)]
echo.
echo merged = f"{out_dir}/{store_id}_all_events.jsonl"
echo with open^(merged, "w"^) as f:
echo     for ev in all_ev:
echo         f.write^(json.dumps^(ev^) + "\n"^)
echo print^(f"  Total events: {len^(all_ev^)}"^)
echo.
echo for i in range^(0, len^(all_ev^), 500^):
echo     batch = all_ev[i:i+500]
echo     r = requests.post^(f"{api_url}/events/ingest", json={"events": batch}, timeout=30^)
echo     d = r.json^(^)
echo     print^(f"  Batch {i//500+1}: accepted={d['accepted']} dup={d['duplicate']} rejected={d['rejected']}"^)
) > "%TMPPY2%"

python "%TMPPY2%" "%S_DIR%" "%S_ID%" %API_URL%
del "%TMPPY2%" >nul 2>&1
goto :EOF

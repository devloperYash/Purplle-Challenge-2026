@echo off
echo ========================================================
echo Starting Store Intelligence Pipeline (Purplle Challenge)
echo ========================================================
echo.

cd /d "%~dp0"
if not exist "events" mkdir "events"

set STORE_ID=STORE_BLR_002
set LAYOUT=data\store_layout.json
FOR /F "tokens=*" %%g IN ('powershell -Command "(Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH\:mm\:ssZ')"') do (SET START_TIME=%%g)
set API_URL=http://localhost:8000/events/ingest

echo [1/5] Processing CAM 1...
if exist "..\CCTV Footage\CAM 1.mp4" (
    python pipeline\detect.py --video "..\CCTV Footage\CAM 1.mp4" --camera-id CAM_FLOOR_01 --store-id %STORE_ID% --layout %LAYOUT% --output events\CAM_FLOOR_01_events.jsonl --clip-start %START_TIME% --skip-frames 3 --conf 0.35 --push-to-api %API_URL%
) else (
    echo CAM 1 footage not found!
)

echo.
echo [2/5] Processing CAM 2...
if exist "..\CCTV Footage\CAM 2.mp4" (
    python pipeline\detect.py --video "..\CCTV Footage\CAM 2.mp4" --camera-id CAM_FLOOR_02 --store-id %STORE_ID% --layout %LAYOUT% --output events\CAM_FLOOR_02_events.jsonl --clip-start %START_TIME% --skip-frames 3 --conf 0.35 --push-to-api %API_URL%
)

echo.
echo [3/5] Processing CAM 3...
if exist "..\CCTV Footage\CAM 3.mp4" (
    python pipeline\detect.py --video "..\CCTV Footage\CAM 3.mp4" --camera-id CAM_ENTRY_01 --store-id %STORE_ID% --layout %LAYOUT% --output events\CAM_ENTRY_01_events.jsonl --clip-start %START_TIME% --skip-frames 3 --conf 0.35 --push-to-api %API_URL%
)

echo.
echo [4/5] Processing CAM 4...
if exist "..\CCTV Footage\CAM 4.mp4" (
    python pipeline\detect.py --video "..\CCTV Footage\CAM 4.mp4" --camera-id CAM_FLOOR_03 --store-id %STORE_ID% --layout %LAYOUT% --output events\CAM_FLOOR_03_events.jsonl --clip-start %START_TIME% --skip-frames 3 --conf 0.35 --push-to-api %API_URL%
)

echo.
echo [5/5] Processing CAM 5...
if exist "..\CCTV Footage\CAM 5.mp4" (
    python pipeline\detect.py --video "..\CCTV Footage\CAM 5.mp4" --camera-id CAM_BILLING_03 --store-id %STORE_ID% --layout %LAYOUT% --output events\CAM_BILLING_03_events.jsonl --clip-start %START_TIME% --skip-frames 3 --conf 0.35 --push-to-api %API_URL%
)

echo.
echo Merging all events...
python -c "
import sys, json, glob
events = []
for path in glob.glob('events/CAM_*_events.jsonl'):
    for line in open(path, encoding='utf-8'):
        if line.strip():
            try: events.append(json.loads(line))
            except: pass
events.sort(key=lambda e: e.get('timestamp', ''))
with open('events/all_events.jsonl', 'w', encoding='utf-8') as f:
    for e in events:
        f.write(json.dumps(e) + '\n')
"
echo Merged events into events/all_events.jsonl

echo.
echo All cameras processed!
pause

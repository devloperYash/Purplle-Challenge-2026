@echo off
set DB_PATH=data/store_intelligence.db
set POS_CSV_PATH=data/Brigade_Bangalore_transactions.csv
set PYTHONPATH=app

echo Starting FastAPI Backend without Docker...
uvicorn app.main:app --host 0.0.0.0 --port 8000
pause

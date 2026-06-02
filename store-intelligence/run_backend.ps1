$env:DB_PATH="data/store_intelligence.db"
$env:POS_CSV_PATH="data/Brigade_Bangalore_transactions.csv"
$env:PYTHONPATH="app"

Write-Host "Starting FastAPI Backend without Docker..."
uvicorn app.main:app --host 0.0.0.0 --port 8000

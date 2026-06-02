import logging
import time
import uuid
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import asyncio

from database import init_db, db_conn
from ingestion import ingest_events, load_pos_transactions_from_csv
from metrics import get_store_metrics
from funnel import get_conversion_funnel
from anomalies import get_anomalies
from health import get_health
from models import (
    IngestRequest, IngestResponse,
    MetricsResponse, FunnelResponse,
    HeatmapResponse, HeatmapZone,
    AnomalyResponse, HealthResponse
)

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)
logger = logging.getLogger("api")

_startup_ts = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Load real POS data on startup if the CSV exists
    pos_csv = os.getenv("POS_CSV_PATH", "data/Brigade_Bangalore_transactions.csv")
    if os.path.exists(pos_csv):
        try:
            loaded = load_pos_transactions_from_csv(pos_csv, "STORE_BLR_002")
            logger.info(f"Loaded {loaded} POS transactions from {pos_csv}")
        except Exception as e:
            logger.warning(f"Could not load POS CSV: {e}")
    yield


app = FastAPI(
    title="Store Intelligence API",
    description="Real-time retail analytics from CCTV footage — Brigade Road Bangalore",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket connection manager for real-time dashboard
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

ws_manager = ConnectionManager()


@app.middleware("http")
async def structured_logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    request.state.trace_id = trace_id
    start = time.monotonic()

    response = await call_next(request)

    latency_ms = round((time.monotonic() - start) * 1000, 1)
    store_id = request.path_params.get("store_id", "-")

    logger.info(
        f"trace_id={trace_id} method={request.method} path={request.url.path} "
        f"store_id={store_id} status={response.status_code} latency_ms={latency_ms}"
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "Something went wrong. Check server logs.",
            "trace_id": getattr(request.state, "trace_id", None),
        }
    )


@app.post("/events/ingest", response_model=IngestResponse, status_code=200)
async def ingest(request: Request, body: IngestRequest):
    trace_id = getattr(request.state, "trace_id", "?")
    result = ingest_events(body, trace_id)

    # Broadcast to dashboard subscribers
    if result.accepted > 0:
        asyncio.create_task(ws_manager.broadcast({
            "type": "events_ingested",
            "count": result.accepted,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }))

    return result


@app.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
async def metrics(store_id: str, window_hours: int = Query(9999, ge=1, le=9999)):
    try:
        return get_store_metrics(store_id, window_hours)
    except Exception as e:
        logger.error(f"Metrics error for {store_id}: {e}", exc_info=True)
        raise HTTPException(503, detail={"error": "service_unavailable", "message": str(e)})


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def funnel(store_id: str, window_hours: int = Query(9999, ge=1, le=9999)):
    try:
        return get_conversion_funnel(store_id, window_hours)
    except Exception as e:
        logger.error(f"Funnel error for {store_id}: {e}", exc_info=True)
        raise HTTPException(503, detail={"error": "service_unavailable", "message": str(e)})


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def heatmap(store_id: str, window_hours: int = Query(9999, ge=1, le=9999)):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    ws = (now - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    we = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with db_conn() as conn:
            rows = conn.execute(
                """
                SELECT zone_id,
                       COUNT(DISTINCT visitor_id) as visit_count,
                       AVG(dwell_ms) as avg_dwell,
                       metadata
                FROM (
                    SELECT zone_id, visitor_id, dwell_ms, '' as metadata
                    FROM events
                    WHERE store_id = ?
                      AND zone_id IS NOT NULL
                      AND is_staff = 0
                      AND timestamp BETWEEN ? AND ?
                ) GROUP BY zone_id
                ORDER BY visit_count DESC
                """,
                (store_id, ws, we)
            ).fetchall()

        if not rows:
            return HeatmapResponse(store_id=store_id, zones=[], data_confidence="LOW")

        # Normalize visit counts 0-100
        max_visits = max(r["visit_count"] for r in rows) or 1
        session_count = sum(r["visit_count"] for r in rows)

        zones = []
        for row in rows:
            score = round((row["visit_count"] / max_visits) * 100, 1)
            zones.append(HeatmapZone(
                zone_id=row["zone_id"],
                sku_zone=None,
                visit_count=row["visit_count"],
                avg_dwell_seconds=round((row["avg_dwell"] or 0) / 1000, 1),
                normalized_score=score,
            ))

        confidence = "HIGH" if session_count >= 20 else ("MEDIUM" if session_count >= 5 else "LOW")
        return HeatmapResponse(store_id=store_id, zones=zones, data_confidence=confidence)

    except Exception as e:
        logger.error(f"Heatmap error for {store_id}: {e}", exc_info=True)
        raise HTTPException(503, detail={"error": "service_unavailable", "message": str(e)})


@app.get("/stores/{store_id}/anomalies", response_model=AnomalyResponse)
async def anomalies(store_id: str):
    try:
        return get_anomalies(store_id)
    except Exception as e:
        logger.error(f"Anomaly check error for {store_id}: {e}", exc_info=True)
        raise HTTPException(503, detail={"error": "service_unavailable", "message": str(e)})


@app.get("/health", response_model=HealthResponse)
async def health():
    return get_health()


@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    # __file__ is store-intelligence/app/main.py, so dashboard is one level up
    dashboard_path = Path(__file__).parent.parent / "dashboard" / "index.html"
    try:
        return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<h1>Dashboard not found</h1><p>Expected at: " + str(dashboard_path) + "</p>", status_code=404)

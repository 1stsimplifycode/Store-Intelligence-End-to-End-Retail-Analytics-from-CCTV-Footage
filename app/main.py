"""
Store Intelligence API - FastAPI entrypoint
"""
import uuid
import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from app.database import init_db
from app.routers import events, metrics, funnel, heatmap, anomalies, health

# Structured logger
logging.basicConfig(
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
    level=logging.INFO
)
logger = logging.getLogger("store_intelligence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info('"Store Intelligence API started"')
    yield
    logger.info('"Store Intelligence API shutting down"')


app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Real-time retail store analytics from CCTV event streams",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    start = time.perf_counter()
    request.state.trace_id = trace_id
    try:
        response: Response = await call_next(request)
    except Exception as exc:
        logger.error(
            f'{{"trace_id":"{trace_id}","endpoint":"{request.url.path}",'
            f'"error":"{str(exc)}"}}'
        )
        raise
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    store_id = request.path_params.get("store_id", "")
    logger.info(
        f'{{"trace_id":"{trace_id}","store_id":"{store_id}",'
        f'"endpoint":"{request.url.path}","method":"{request.method}",'
        f'"status_code":{response.status_code},"latency_ms":{latency_ms}}}'
    )
    response.headers["X-Trace-ID"] = trace_id
    return response


app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(metrics.router, prefix="/stores", tags=["metrics"])
app.include_router(funnel.router, prefix="/stores", tags=["funnel"])
app.include_router(heatmap.router, prefix="/stores", tags=["heatmap"])
app.include_router(anomalies.router, prefix="/stores", tags=["anomalies"])
app.include_router(health.router, tags=["health"])

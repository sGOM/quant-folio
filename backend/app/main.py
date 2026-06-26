"""FastAPI web 진입점 — REST/WS API. 매매 로직은 포함하지 않는다."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    auth,
    backtests,
    engine,
    kis,
    strategies,
    symbols,
    trading,
    ws,
)
from app.core.config import settings
from app.core.redis import redis_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("web 시작 — KIS_ENV=%s (모의투자=%s)", settings.KIS_ENV, settings.is_paper_trading)
    yield
    await redis_client.aclose()


app = FastAPI(
    title="QuantFolio API",
    version="0.1.0",
    description="국내 주식 퀀트 백테스팅·자동매매 플랫폼",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(kis.router)
app.include_router(strategies.router)
app.include_router(backtests.router)
app.include_router(engine.router)
app.include_router(trading.router)
app.include_router(symbols.router)
app.include_router(ws.router)


@app.get("/health", tags=["meta"])
async def health():
    """서비스/Redis 헬스체크."""
    redis_ok = False
    try:
        redis_ok = await redis_client.ping()
    except Exception as e:  # noqa: BLE001
        logger.warning("Redis ping 실패: %s", e)
    return {
        "status": "ok",
        "redis": redis_ok,
        "kis_env": settings.KIS_ENV,
        "paper_trading": settings.is_paper_trading,
    }

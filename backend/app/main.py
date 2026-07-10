"""FastAPI web 진입점 — REST/WS API. 매매 로직은 포함하지 않는다."""
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    auth,
    backtests,
    engine,
    kis,
    metrics,
    recommend,
    screener,
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """처리되지 않은 예외를 잡아 500 응답을 만든다.

    트레이스백은 항상 서버 로그에 남긴다. 응답 본문은 기본적으로 일반화된
    메시지만 담아 내부 구조·경로·시크릿 단서 노출을 막는다. 단, DEBUG_ERRORS
    가 켜져 있으면(운영 외 환경에서만 허용) 예외 타입·메시지·트레이스백을
    응답에 그대로 실어 디버깅을 돕는다.

    참고: HTTPException·RequestValidationError 는 FastAPI 가 자체 핸들러로
    먼저 처리하므로 여기까지 오지 않는다(개별 detail 은 그대로 반환됨).
    """
    logger.exception("처리되지 않은 예외: %s %s", request.method, request.url.path)

    if settings.DEBUG_ERRORS:
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal Server Error",
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "path": request.url.path,
                    "traceback": traceback.format_exception(
                        type(exc), exc, exc.__traceback__
                    ),
                },
            },
        )

    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )

app.include_router(auth.router)
app.include_router(kis.router)
app.include_router(strategies.router)
app.include_router(backtests.router)
app.include_router(engine.router)
app.include_router(trading.router)
app.include_router(symbols.router)
app.include_router(metrics.router)
app.include_router(recommend.router)
app.include_router(screener.router)
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

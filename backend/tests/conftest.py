"""테스트 공통 설정 — 앱 모듈 import 전에 필수 환경변수를 주입한다."""
import base64
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef")
os.environ.setdefault(
    "CREDENTIAL_ENC_KEY", base64.urlsafe_b64encode(b"0" * 32).decode()
)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://quant:quant@localhost:5432/quant")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("KIS_ENV", "vts")

"""애플리케이션 설정 — 환경변수에서 로드 (pydantic-settings)."""
import os
from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 코드에 박힌 추측 가능한 placeholder. 이 값들은 운영에서 절대 허용하지 않는다.
_PLACEHOLDER_SECRETS = {
    "",
    "change-me-dev-only-secret-key",
    "change-me-generate-a-fernet-key-base64-32bytes=",
}

# `<FIELD>_FILE` 간접 참조를 허용하는 민감 필드.
# 평문 env/.env 대신 파일(예: Docker secret, /run/secrets/...)에서 값을 읽어
# `docker inspect`·이미지 레이어·프로세스 환경에 비밀이 남지 않게 한다.
_SECRET_FILE_FIELDS = (
    "SECRET_KEY",
    "CREDENTIAL_ENC_KEY",
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "TOSS_APP_KEY",
    "TOSS_APP_SECRET",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @model_validator(mode="before")
    @classmethod
    def _load_secret_files(cls, data):
        """`<FIELD>_FILE` 가 가리키는 파일이 있으면 그 내용을 해당 필드값으로 사용한다.

        시크릿 파일(Docker secret 등)이 평문 env/.env 보다 우선한다 — 비밀을
        파일 한 곳에만 두고 환경변수 노출면을 줄이기 위함이다. 파일 내용은
        끝의 개행을 제거(strip)해 사용한다.
        """
        if not isinstance(data, dict):
            return data
        for name in _SECRET_FILE_FIELDS:
            path = os.environ.get(f"{name}_FILE")
            if path and os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    data[name] = fh.read().strip()
        return data

    # --- Database ---
    DATABASE_URL: str = "postgresql+asyncpg://quant:quant@db:5432/quant"

    # --- Redis ---
    REDIS_URL: str = "redis://redis:6379/0"

    # --- 실행 환경 ---
    # dev: 누락된 시크릿을 임시 생성해 부팅 허용(로컬 편의). prod: 누락 시 부팅 거부.
    APP_ENV: Literal["dev", "prod"] = "dev"

    # --- 보안 ---
    # 기본값 없음 — 환경변수/시크릿 매니저로만 주입한다. 미설정 시 부팅 실패.
    SECRET_KEY: str = ""
    # 로그인 세션(서버측, Redis) 유효기간. 활동이 있으면 슬라이딩 갱신된다.
    SESSION_TTL_MINUTES: int = 60 * 24 * 14  # 14일
    # KIS 자격증명 암호화용 Fernet 키 (base64, 32바이트)
    CREDENTIAL_ENC_KEY: str = ""

    # 쿠키 보안 — prod 에서는 항상 Secure. 교차 출처면 SameSite=None 필요.
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    COOKIE_DOMAIN: str | None = None

    # --- KIS API ---
    KIS_ENV: Literal["vts", "prod"] = "vts"
    KIS_BASE_URL_VTS: str = "https://openapivts.koreainvestment.com:29443"
    KIS_BASE_URL_PROD: str = "https://openapi.koreainvestment.com:9443"
    # KIS 실시간 시세는 TLS(wss) 로 연결한다 — 평문 ws 금지.
    KIS_WS_URL_VTS: str = "wss://ops.koreainvestment.com:31000"
    KIS_WS_URL_PROD: str = "wss://ops.koreainvestment.com:21000"

    # 단일 운영자(개인) 편의용 기본 자격증명.
    # 사용자가 앱에서 등록(DB)하지 않은 경우 이 값을 폴백으로 사용한다.
    # 멀티 유저 운영에서는 비워 두고 사용자별 등록 API 를 사용하는 것이 안전하다.
    KIS_APP_KEY: str = ""
    KIS_APP_SECRET: str = ""
    KIS_ACCOUNT_NO: str = ""  # 'CANO-PRDT' 형식 (예: 50012345-01)

    # --- 토스증권 Open API ---
    # 토스는 단일 운영 도메인만 제공(모의투자 환경 없음), REST 전용(WS 미지원).
    # 향후 WS 가 추가되면 TOSS_WS_URL 을 더해 PriceFeed 에 연동한다.
    TOSS_BASE_URL: str = "https://openapi.tossinvest.com"
    # 토스 기본 자격증명(개인 편의용 폴백). app_key=client_id, account_no=accountSeq.
    TOSS_APP_KEY: str = ""
    TOSS_APP_SECRET: str = ""
    TOSS_ACCOUNT_NO: str = ""

    # --- CORS ---
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    @field_validator("SECRET_KEY", "CREDENTIAL_ENC_KEY", mode="after")
    @classmethod
    def _reject_placeholder(cls, v: str, info) -> str:
        """시크릿 필드에 코드에 박힌 placeholder 값이 들어오면 거부한다.

        빈 값은 환경별 처리를 위해 통과시키고(model_validator 담당),
        추측 가능한 placeholder 만 차단한다.
        """
        # 비어 있으면 model_validator 에서 환경별로 처리하므로 여기선 placeholder만 차단.
        if v and v in _PLACEHOLDER_SECRETS:
            raise ValueError(
                f"{info.field_name} 에 placeholder 값을 사용할 수 없습니다. "
                "안전한 시크릿을 환경변수로 주입하세요."
            )
        return v

    @model_validator(mode="after")
    def _ensure_secrets(self) -> "Settings":
        """시크릿·쿠키·암호화 키의 환경별 안전성을 부팅 시점에 강제한다.

        - prod: 필수 시크릿 누락·비보안 쿠키면 부팅 거부.
        - dev: 누락 시크릿은 임시 키를 생성해 부팅 허용(로컬 편의).
        - 공통: CREDENTIAL_ENC_KEY 가 유효한 Fernet 키인지 검증.
        """
        missing = [
            name
            for name in ("SECRET_KEY", "CREDENTIAL_ENC_KEY")
            if not getattr(self, name)
        ]
        if missing:
            if self.APP_ENV == "prod":
                raise ValueError(
                    f"운영(prod) 환경에서 필수 시크릿이 누락되었습니다: {', '.join(missing)}"
                )
            # dev: 부팅 편의를 위해 임시 키 생성(프로세스 한정, 재시작 시 무효).
            import logging

            logger = logging.getLogger(__name__)
            for name in missing:
                if name == "CREDENTIAL_ENC_KEY":
                    object.__setattr__(self, name, Fernet.generate_key().decode())
                else:
                    object.__setattr__(self, name, Fernet.generate_key().decode())
                logger.warning(
                    "%s 미설정 — dev 임시 키 생성. 운영에서는 반드시 환경변수로 주입하세요.",
                    name,
                )

        # prod 에서 KIS 실거래인데 쿠키가 비보안이면 토큰 탈취 위험 → 거부.
        if self.APP_ENV == "prod" and not self.COOKIE_SECURE:
            raise ValueError("운영 환경에서는 COOKIE_SECURE=true 여야 합니다.")

        # Fernet 키 형식 검증(잘못된 키는 첫 암호화 시점에야 터지므로 부팅 시 확인).
        try:
            Fernet(self.CREDENTIAL_ENC_KEY.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise ValueError(
                "CREDENTIAL_ENC_KEY 가 유효한 Fernet 키(base64 32바이트)가 아닙니다."
            ) from e
        return self

    @property
    def kis_base_url(self) -> str:
        """현재 KIS_ENV 에 해당하는 REST 기본 URL(prod/모의투자 분기)."""
        return self.KIS_BASE_URL_PROD if self.KIS_ENV == "prod" else self.KIS_BASE_URL_VTS

    @property
    def kis_ws_url(self) -> str:
        """현재 KIS_ENV 에 해당하는 실시간 시세 WebSocket URL(wss)."""
        return self.KIS_WS_URL_PROD if self.KIS_ENV == "prod" else self.KIS_WS_URL_VTS

    @property
    def is_paper_trading(self) -> bool:
        """모의투자 여부. 기본값은 안전하게 모의투자."""
        return self.KIS_ENV != "prod"


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴을 반환한다(lru_cache 로 프로세스당 1회 로드)."""
    return Settings()


# 모듈 임포트 시 1회 로드되는 전역 설정 인스턴스.
settings = get_settings()

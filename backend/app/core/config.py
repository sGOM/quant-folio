"""애플리케이션 설정 — 환경변수에서 로드 (pydantic-settings)."""
import os
from decimal import Decimal
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
    "KRX_ID",
    "KRX_PW",
    "OPENDART_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "S3_BACKUP_ACCESS_KEY_ID",
    "S3_BACKUP_SECRET_ACCESS_KEY",
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
    # 실시간 체결통보(H0STCNI0/H0STCNI9) 구독의 tr_key. KIS 계좌의 HTS ID(로그인 ID).
    # 미설정이면 체결통보 리스너(engine/fill_notice.py)는 안전하게 비활성(no-op)된다.
    KIS_HTS_ID: str | None = None

    # --- 실전(prod) 전환 안전 게이트 ---
    # KIS_ENV=prod(실전) 는 실제 자금이 즉시 나가므로, 실수로 켜지는 사고를 구조적으로
    # 막기 위한 2단계 승인 플래그. 기본값은 항상 False(안전측) — prod 로 전환하려면
    # 명시적으로 True 로 주입해야 하며, 승인 없이 prod 면 프로세스 기동이 거부된다
    # (아래 _ensure_prod_approval 검증기). 실전 게이트(app/services/live_gate.py)도
    # 이 플래그가 꺼져 있으면 주문을 차단한다.
    KIS_PROD_APPROVED: bool = False
    # 일일 누적 주문 금액 상한(원). 당일 체결 합계가 이 값을 넘기면 실전 게이트가
    # 신규 주문을 차단한다. None/0 이면 미적용(1회 상한은 RiskLimit.max_position_size).
    KIS_DAILY_ORDER_NOTIONAL_CAP: Decimal | None = None

    # --- 토스증권 Open API ---
    # 토스는 단일 운영 도메인만 제공(모의투자 환경 없음), REST 전용(WS 미지원).
    # 향후 WS 가 추가되면 TOSS_WS_URL 을 더해 PriceFeed 에 연동한다.
    TOSS_BASE_URL: str = "https://openapi.tossinvest.com"
    # 토스 기본 자격증명(개인 편의용 폴백). app_key=client_id, account_no=accountSeq.
    TOSS_APP_KEY: str = ""
    TOSS_APP_SECRET: str = ""
    TOSS_ACCOUNT_NO: str = ""

    # --- KRX 데이터 포털 (지표 화면용 일괄 시세) ---
    # pykrx 1.2.x 가 data.krx.co.kr 로그인에 사용한다(전 종목 펀더멘털·시총·업종지수).
    # 시크릿 파일(/run/secrets/krx_id, krx_pw)로 주입하며, 아래 검증기가 os.environ
    # 으로 내보내 pykrx(os.getenv)가 읽게 한다. 미설정이면 일괄 시세만 비활성(개별
    # 종목 시세는 FinanceDataReader 로 계속 동작).
    KRX_ID: str = ""
    KRX_PW: str = ""

    # --- OpenDART (금융감독원 전자공시 API) — 준비/스캐폴딩 ---
    # 재무제표 기반 지표(ROE·부채비율·영업이익/순이익·FCF 등)를 공급해 pykrx의
    # PER/PBR/DIV를 보완하기 위한 소스. 무료 API 키 발급 후 시크릿 파일
    # (secrets/opendart_api_key.txt → OPENDART_API_KEY_FILE)로 주입한다.
    # 미설정이면 OpenDART 조회는 전부 비활성(None 반환)되고 기존 동작에 영향 없음.
    # 발급: https://opendart.fss.or.kr (일 20,000건). 배선 계획: docs/opendart-integration.md
    OPENDART_API_KEY: str = ""
    OPENDART_BASE_URL: str = "https://opendart.fss.or.kr/api"

    # --- 텔레그램 크리티컬 알림 ---
    # 24h 무인 자동매매에서 MDD 킬스위치·러너 연속실패 등 critical 알림을 앱 미접속
    # 시에도 받기 위한 외부 채널. 봇 생성: @BotFather. 시크릿 파일(secrets/telegram_bot_token.txt,
    # secrets/telegram_chat_id.txt)로 주입. 둘 다 비어있지 않을 때만 활성화되고, 미설정이면
    # 기존 앱 내(WS) 알림만 동작(영향 없음).
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # --- DB 백업 오프사이트 복제(§10, opt-in) ---
    # 야간 pg_dump 백업(named volume db_backups)은 서버 로컬에만 있어 디스크 손상 시
    # 복구 불가(docs/db-backup.md "오프사이트 보관 권장"). 성공한 백업을 S3 호환 스토리지
    # (AWS S3/Cloudflare R2/Backblaze B2/MinIO 등)에 추가 업로드해 이를 해소한다.
    # S3_BACKUP_ENDPOINT_URL 은 AWS 는 비워두고(기본 AWS 엔드포인트), R2/B2/MinIO 등은
    # 해당 서비스의 S3 호환 엔드포인트를 넣는다. 버킷 미설정이면 업로드는 통째로 비활성
    # (기존 로컬 전용 백업 동작에 영향 없음).
    S3_BACKUP_BUCKET: str = ""
    S3_BACKUP_ENDPOINT_URL: str = ""
    S3_BACKUP_REGION: str = "auto"
    S3_BACKUP_PREFIX: str = "quantfolio-db-backups/"
    S3_BACKUP_ACCESS_KEY_ID: str = ""
    S3_BACKUP_SECRET_ACCESS_KEY: str = ""

    # --- CORS ---
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    # --- 에러 응답 상세도 ---
    # True 면 처리되지 않은 예외의 타입·메시지·트레이스백을 HTTP 응답 본문에
    # 그대로 담아 반환한다(디버깅 편의). 내부 구조·경로·시크릿 단서가 노출될 수
    # 있으므로 기본은 False. prod 환경에서는 아래 검증기가 강제로 False 로 되돌린다.
    DEBUG_ERRORS: bool = False

    @model_validator(mode="after")
    def _export_krx_credentials(self) -> "Settings":
        """KRX 자격증명을 os.environ 으로 내보낸다(pykrx 가 os.getenv 로 직접 읽음).

        시크릿 파일/평문 env 어느 경로로 들어왔든, 비어 있지 않을 때만 환경변수로
        설정해 pykrx 의 KRX 데이터 포털 자동 로그인을 활성화한다.
        """
        if self.KRX_ID:
            os.environ["KRX_ID"] = self.KRX_ID
        if self.KRX_PW:
            os.environ["KRX_PW"] = self.KRX_PW
        return self

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

        # prod 에서 상세 에러 노출은 정보 유출 위험 → 켜져 있어도 강제로 끈다.
        if self.APP_ENV == "prod" and self.DEBUG_ERRORS:
            import logging

            object.__setattr__(self, "DEBUG_ERRORS", False)
            logging.getLogger(__name__).warning(
                "운영(prod) 환경에서는 DEBUG_ERRORS 를 사용할 수 없어 False 로 강제합니다."
            )

        # Fernet 키 형식 검증(잘못된 키는 첫 암호화 시점에야 터지므로 부팅 시 확인).
        try:
            Fernet(self.CREDENTIAL_ENC_KEY.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise ValueError(
                "CREDENTIAL_ENC_KEY 가 유효한 Fernet 키(base64 32바이트)가 아닙니다."
            ) from e
        return self

    @model_validator(mode="after")
    def _ensure_prod_approval(self) -> "Settings":
        """실전(KIS_ENV=prod) 인데 2단계 승인이 없으면 프로세스 기동을 거부한다.

        실전은 실제 자금이 즉시 나가므로, 승인 플래그(KIS_PROD_APPROVED)를 명시적으로
        켜지 않은 채 prod 로 전환되면 부팅 자체를 막아 실수 전환을 구조적으로 차단한다
        (APP_ENV prod 검증과 동일한 '안전측 기본' 방침). 런타임 실전 게이트
        (app/services/live_gate.py)와 이중 방어를 이룬다.
        """
        if self.KIS_ENV == "prod" and not self.KIS_PROD_APPROVED:
            raise ValueError(
                "KIS_ENV=prod(실전)인데 KIS_PROD_APPROVED 가 승인(true)되지 않았습니다. "
                "실전 전환은 명시적 2단계 승인이 필요합니다 — KIS_PROD_APPROVED=true 를 주입하세요."
            )
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

    @property
    def has_opendart(self) -> bool:
        """OpenDART API 키가 주입되어 재무 데이터 조회가 가능한지 여부."""
        return bool(self.OPENDART_API_KEY.strip())

    @property
    def has_telegram(self) -> bool:
        """텔레그램 봇 토큰·채팅ID 가 모두 주입되어 외부 알림 발송이 가능한지 여부."""
        return bool(self.TELEGRAM_BOT_TOKEN.strip() and self.TELEGRAM_CHAT_ID.strip())

    @property
    def has_s3_backup(self) -> bool:
        """S3 백업 버킷·자격증명이 모두 주입되어 오프사이트 업로드가 가능한지 여부(§10)."""
        return bool(
            self.S3_BACKUP_BUCKET.strip()
            and self.S3_BACKUP_ACCESS_KEY_ID.strip()
            and self.S3_BACKUP_SECRET_ACCESS_KEY.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴을 반환한다(lru_cache 로 프로세스당 1회 로드)."""
    return Settings()


# 모듈 임포트 시 1회 로드되는 전역 설정 인스턴스.
settings = get_settings()

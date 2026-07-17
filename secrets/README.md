# secrets/

비밀(마스터 키·증권사 API 키)을 **평문 `.env` 대신 파일**로 보관하는 디렉터리입니다.
docker compose 가 이 파일들을 `/run/secrets/*`(tmpfs)로 마운트하고, 백엔드 설정의
`<FIELD>_FILE` 로더가 읽어 사용합니다. 따라서 비밀이 `docker inspect`·이미지 레이어·
프로세스 환경(`/proc`)에 남지 않습니다.

> ⚠️ 이 디렉터리의 `*.txt` 는 `.gitignore` 로 커밋에서 제외됩니다. 절대 커밋하지 마세요.

## 필요한 파일

| 파일 | 내용 | 비고 |
|------|------|------|
| `secret_key.txt` | JWT 서명 키 | `openssl rand -hex 32` |
| `credential_enc_key.txt` | DB 자격증명 Fernet 암호화 키 | 아래 생성 명령 |
| `kis_app_key.txt` | (선택) KIS app_key 폴백 | 미사용 시 빈 파일 |
| `kis_app_secret.txt` | (선택) KIS app_secret 폴백 | 미사용 시 빈 파일 |
| `toss_app_key.txt` | (선택) 토스 client_id 폴백 | 미사용 시 빈 파일 |
| `toss_app_secret.txt` | (선택) 토스 client_secret 폴백 | 미사용 시 빈 파일 |
| `krx_id.txt` | (선택) KRX 데이터 포털 로그인 ID | 지표 화면용. 미사용 시 빈 파일 |
| `krx_pw.txt` | (선택) KRX 데이터 포털 비밀번호 | 지표 화면용. 미사용 시 빈 파일 |
| `opendart_api_key.txt` | (선택) OpenDART 인증키 | 재무데이터용(준비/미배선). 미사용 시 빈 파일 |
| `telegram_bot_token.txt` | (선택) 텔레그램 봇 토큰 | critical 알림 외부 발송용. 미사용 시 빈 파일 |
| `telegram_chat_id.txt` | (선택) 텔레그램 채팅 ID | 봇 토큰과 함께 둘 다 설정해야 활성화. 미사용 시 빈 파일 |
| `s3_backup_access_key_id.txt` | (선택) S3 백업 오프사이트 복제 액세스 키 | 야간 DB 백업 추가 반출용(§10). 미사용 시 빈 파일 |
| `s3_backup_secret_access_key.txt` | (선택) S3 백업 오프사이트 복제 시크릿 키 | 액세스 키와 함께 둘 다 설정해야 활성화. 미사용 시 빈 파일 |

브로커 키 폴백을 쓰지 않고 앱(웹 UI)에서 사용자별로 등록한다면 브로커 파일들은
빈 파일로 두면 됩니다(파일 자체는 존재해야 compose 가 기동됩니다).

## 생성 예시

```bash
mkdir -p secrets
openssl rand -hex 32 > secrets/secret_key.txt
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > secrets/credential_enc_key.txt
# 미사용 브로커 키는 빈 파일로 생성
: > secrets/kis_app_key.txt;  : > secrets/kis_app_secret.txt
: > secrets/toss_app_key.txt; : > secrets/toss_app_secret.txt
# KRX 데이터 포털(지표 화면용). 무료 가입 https://data.krx.co.kr — 미사용 시 빈 파일
: > secrets/krx_id.txt; : > secrets/krx_pw.txt
# OpenDART 재무데이터(준비/미배선). 무료 발급 https://opendart.fss.or.kr — 미사용 시 빈 파일
: > secrets/opendart_api_key.txt
# 텔레그램 critical 알림(선택). @BotFather 로 봇 생성 후 토큰 발급, 채팅 ID는
# 봇과 대화 시작 후 https://api.telegram.org/bot<TOKEN>/getUpdates 로 확인 — 미사용 시 빈 파일
: > secrets/telegram_bot_token.txt; : > secrets/telegram_chat_id.txt
# S3 백업 오프사이트 복제(선택, §10). AWS S3/Cloudflare R2/Backblaze B2/MinIO 등
# S3 호환 스토리지의 액세스 키 — 미사용 시 빈 파일(로컬 백업만 동작, 영향 없음)
: > secrets/s3_backup_access_key_id.txt; : > secrets/s3_backup_secret_access_key.txt
```

> 🔁 `credential_enc_key.txt` 를 교체하면 기존에 암호화 저장된 DB 자격증명을
> 더 이상 복호화할 수 없습니다. 키를 바꾸려면 자격증명 재등록 또는 재암호화가 필요합니다.

## 파일 권한(선택, 권장)

```bash
chmod 600 secrets/*.txt
```

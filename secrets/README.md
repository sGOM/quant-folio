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
```

> 🔁 `credential_enc_key.txt` 를 교체하면 기존에 암호화 저장된 DB 자격증명을
> 더 이상 복호화할 수 없습니다. 키를 바꾸려면 자격증명 재등록 또는 재암호화가 필요합니다.

## 파일 권한(선택, 권장)

```bash
chmod 600 secrets/*.txt
```

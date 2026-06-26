"""`<FIELD>_FILE` 간접 참조(시크릿 파일) 로더 검증.

Docker secret 처럼 파일에 담긴 비밀을 평문 env 보다 우선 사용하는지 확인한다.
"""
import base64

from app.core.config import Settings


def _valid_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def test_secret_file_overrides_env(monkeypatch, tmp_path):
    """CREDENTIAL_ENC_KEY_FILE 의 파일 내용이 평문 env 값을 덮어쓴다."""
    file_key = _valid_fernet_key()
    p = tmp_path / "cred.key"
    p.write_text(file_key + "\n", encoding="utf-8")  # 끝 개행은 strip 되어야 함

    # 평문 env 에는 다른(유효한) 키를 둔다 → 파일이 우선해야 한다.
    monkeypatch.setenv("CREDENTIAL_ENC_KEY", _valid_fernet_key())
    monkeypatch.setenv("CREDENTIAL_ENC_KEY_FILE", str(p))
    monkeypatch.setenv("SECRET_KEY", "x" * 40)

    s = Settings(_env_file=None)
    assert s.CREDENTIAL_ENC_KEY == file_key  # 개행 제거 + 파일 우선


def test_missing_secret_file_falls_back_to_env(monkeypatch, tmp_path):
    """파일이 없으면 평문 env 값을 그대로 사용한다."""
    env_key = _valid_fernet_key()
    monkeypatch.setenv("CREDENTIAL_ENC_KEY", env_key)
    monkeypatch.setenv("CREDENTIAL_ENC_KEY_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("SECRET_KEY", "y" * 40)

    s = Settings(_env_file=None)
    assert s.CREDENTIAL_ENC_KEY == env_key


def test_broker_key_from_file(monkeypatch, tmp_path):
    """브로커 폴백 키도 파일에서 읽는다."""
    p = tmp_path / "kis.key"
    p.write_text("KISKEY123", encoding="utf-8")
    monkeypatch.setenv("KIS_APP_KEY_FILE", str(p))
    monkeypatch.setenv("SECRET_KEY", "z" * 40)
    monkeypatch.setenv("CREDENTIAL_ENC_KEY", _valid_fernet_key())

    s = Settings(_env_file=None)
    assert s.KIS_APP_KEY == "KISKEY123"

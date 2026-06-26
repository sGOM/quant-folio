"""보안 함수 라운드트립 검증."""
from app.core import security as s


def test_password_hash_roundtrip():
    h = s.hash_password("supersecret123")
    assert h != "supersecret123"
    assert s.verify_password("supersecret123", h)
    assert not s.verify_password("wrong-password", h)


def test_credential_encryption_roundtrip():
    cipher = s.encrypt_secret("KIS-APP-KEY-XYZ")
    assert cipher != "KIS-APP-KEY-XYZ"  # 평문 노출 금지
    assert s.decrypt_secret(cipher) == "KIS-APP-KEY-XYZ"

"""보안 유틸 — 비밀번호 해싱, KIS 자격증명 암호화.

로그인 인증은 JWT 가 아니라 서버측 세션(``app.core.session``)으로 처리한다.
"""
import bcrypt
from cryptography.fernet import Fernet

from app.core.config import settings

# ─────────────────────────── 비밀번호 ───────────────────────────


def hash_password(password: str) -> str:
    """비밀번호를 bcrypt 로 해싱한다(매번 새 salt 생성).

    :param password: 평문 비밀번호
    :return: 저장 가능한 해시 문자열(salt 포함)
    """
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """평문 비밀번호가 해시와 일치하는지 검증한다.

    :param password: 검증할 평문 비밀번호
    :param password_hash: 저장된 bcrypt 해시
    :return: 일치하면 True. 해시 형식이 잘못되면 False
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# ───────────────────── KIS 자격증명 암호화 ─────────────────────
# KIS app_key/secret 은 평문 저장·로깅 금지. Fernet 대칭키로 암호화한다.

_fernet = Fernet(settings.CREDENTIAL_ENC_KEY.encode("utf-8"))


def encrypt_secret(plaintext: str) -> str:
    """KIS 자격증명 등 민감 문자열을 Fernet 으로 암호화한다.

    :param plaintext: 평문(예: KIS app_secret)
    :return: 암호문(저장 가능 문자열)
    """
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """encrypt_secret 로 암호화한 문자열을 복호화한다.

    :param ciphertext: 암호문
    :return: 복원된 평문
    :raises cryptography.fernet.InvalidToken: 키 불일치·위변조 시
    """
    return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")

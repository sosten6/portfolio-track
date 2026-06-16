"""
crypto.py — Fernet encrypt/decrypt for API keys at rest
"""
from cryptography.fernet import Fernet
import config

if not config.ENCRYPTION_KEY:
    raise ValueError(
        "ENCRYPTION_KEY not set. Generate one with:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

_fernet = Fernet(config.ENCRYPTION_KEY.encode())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except Exception as exc:
        raise ValueError(f"Decryption failed: {exc}")
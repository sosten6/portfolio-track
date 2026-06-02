"""
crypto.py
─────────
Symmetric encryption for sensitive database fields (exchange API keys/secrets).
Uses Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` package.

Generate a key once and store it in your .env:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from cryptography.fernet import Fernet, InvalidToken
from config import ENCRYPTION_KEY

# If no key is configured we operate in "plaintext mode" (dev / demo only).
_fernet: Fernet | None = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns a base64-safe token string."""
    if _fernet is None:
        return plaintext   # dev mode — no encryption
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token back to plaintext."""
    if _fernet is None:
        return token       # dev mode
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt credential — wrong encryption key or corrupted data.")
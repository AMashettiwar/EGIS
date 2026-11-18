from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature


DEFAULT_PRIVATE_KEY = Path("keys/demo_signing_key.pem")
DEFAULT_PUBLIC_KEY = Path("keys/public_key.pem")


def ensure_demo_keypair(private_path: Path = DEFAULT_PRIVATE_KEY, public_path: Path = DEFAULT_PUBLIC_KEY) -> None:
    private_path = Path(private_path)
    public_path = Path(public_path)
    if private_path.exists() and public_path.exists():
        return
    if not private_path.exists():
        raise FileNotFoundError(f"signing key is not provisioned: {private_path}")
    private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("signing key is not Ed25519")
    derived_public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public_path.exists() and public_path.read_bytes() != derived_public:
        raise ValueError("published public key does not match signing key")
    if not public_path.exists():
        raise FileNotFoundError(f"published public key is not provisioned: {public_path}")


def sign(unsigned_bytes: bytes, private_path: Path = DEFAULT_PRIVATE_KEY) -> str:
    ensure_demo_keypair(private_path, DEFAULT_PUBLIC_KEY)
    private = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("signing key is not Ed25519")
    return base64.b64encode(private.sign(unsigned_bytes)).decode("ascii")


def verify(unsigned_bytes: bytes, signature: str, public_path: Path = DEFAULT_PUBLIC_KEY) -> bool:
    try:
        public = serialization.load_pem_public_key(Path(public_path).read_bytes())
        if not isinstance(public, Ed25519PublicKey):
            return False
        public.verify(base64.b64decode(signature.encode("ascii")), unsigned_bytes)
        return True
    except (OSError, ValueError, TypeError, AttributeError, InvalidSignature):
        return False

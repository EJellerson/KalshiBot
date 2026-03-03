from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from weather_arb.connectors.kalshi import KalshiAuthClient, KalshiCredentials


def test_rsa_pss_signature_roundtrip(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "kalshi_key.pem"
    key_path.write_bytes(pem)

    client = KalshiAuthClient(
        credentials=KalshiCredentials(api_key="abc", rsa_private_key_path=str(key_path)),
        base_url="https://example.com",
    )

    ts = "1700000000000"
    method = "GET"
    path = "/markets"
    sig_b64 = client.build_signature(ts, method, path)
    sig = base64.b64decode(sig_b64)

    message = f"{ts}{method}{path}".encode("utf-8")
    pub = key.public_key()
    pub.verify(
        sig,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_canonical_path_excludes_query():
    path = KalshiAuthClient.canonical_signing_path(
        "/markets",
        params={"status": "open"},
        api_base_path="/trade-api/v2",
    )
    assert path == "/trade-api/v2/markets"


def test_canonical_path_without_api_prefix():
    path = KalshiAuthClient.canonical_signing_path("/markets", params={"status": "open"})
    assert path == "/markets"


def test_rsa_inline_pem_supported():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    client = KalshiAuthClient(
        credentials=KalshiCredentials(
            api_key="abc",
            rsa_private_key_path="",
            rsa_private_key_pem=pem.decode("utf-8"),
        ),
        base_url="https://example.com",
    )
    sig = client.build_signature("1700000000000", "GET", "/markets")
    assert isinstance(sig, str)
    assert len(sig) > 10

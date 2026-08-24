"""Unit tests for the device-link PKI lifecycle."""

import builtins
import datetime
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import em_pki


def _read_cert(path):
    with open(path, "rb") as handle:
        return x509.load_pem_x509_certificate(handle.read())


def _read_key(path):
    with open(path, "rb") as handle:
        return serialization.load_pem_private_key(handle.read(), password=None)


def test_tls_dir_uses_environment_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-tls"
    monkeypatch.setenv("TLS_DIR", str(override))

    assert em_pki._tls_dir(str(tmp_path / "controller.db")) == str(override)


def test_tls_dir_defaults_next_to_database(tmp_path):
    assert em_pki._tls_dir(str(tmp_path / "controller.db")) == str(
        tmp_path / "tls"
    )
    assert em_pki._tls_dir("controller.db") == os.path.join(".", "tls")


def test_generate_writes_a_valid_chain_with_expected_identity(tmp_path):
    em_pki._generate(str(tmp_path))

    files = {path.name for path in tmp_path.iterdir()}
    assert files == {"ca.pem", "ca.key", "server.pem", "server.key"}
    for name in files:
        assert oct((tmp_path / name).stat().st_mode & 0o777) == "0o600"

    ca = _read_cert(tmp_path / "ca.pem")
    server = _read_cert(tmp_path / "server.pem")
    ca_key = _read_key(tmp_path / "ca.key")
    server_key = _read_key(tmp_path / "server.key")

    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)
    assert isinstance(server_key, ec.EllipticCurvePrivateKey)
    assert ca_key.curve.name == "secp256r1"
    assert server_key.curve.name == "secp256r1"
    assert ca.subject == ca.issuer
    assert ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        "EchoMuse Controller CA"
    )
    assert server.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        em_pki.TLS_SERVER_NAME
    )
    assert server.issuer == ca.subject

    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.value.get_values_for_type(x509.DNSName) == [em_pki.TLS_SERVER_NAME]
    basic_constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic_constraints.value.ca is True
    server_constraints = server.extensions.get_extension_for_class(x509.BasicConstraints)
    assert server_constraints.value.ca is False
    eku = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(eku.value) == [ExtendedKeyUsageOID.SERVER_AUTH]

    now = datetime.datetime.now(datetime.timezone.utc)
    # cryptography 44 exposes *_utc; support the older local test install too
    # while asserting the same timezone-aware validity contract.
    not_before = getattr(ca, "not_valid_before_utc", ca.not_valid_before.replace(tzinfo=datetime.timezone.utc))
    not_after = getattr(ca, "not_valid_after_utc", ca.not_valid_after.replace(tzinfo=datetime.timezone.utc))
    assert not_before < now - datetime.timedelta(days=3649)
    assert not_after > now + datetime.timedelta(days=365 * 24)

    # Verify the leaf signature with the generated CA public key. This checks
    # the chain relationship rather than only comparing issuer name strings.
    ca.public_key().verify(
        server.signature,
        server.tbs_certificate_bytes,
        ec.ECDSA(SHA256()),
    )


def test_ensure_pki_generates_lazily_and_reuses_existing_files(tmp_path):
    db_path = str(tmp_path / "echo.db")
    tls_dir = tmp_path / "tls"

    assert em_pki.ensure_pki(db_path) == str(tls_dir)
    original = {
        name: (tls_dir / name).read_bytes()
        for name in ("ca.pem", "ca.key", "server.pem", "server.key")
    }
    assert em_pki.ensure_pki(db_path) == str(tls_dir)
    assert {
        name: (tls_dir / name).read_bytes()
        for name in original
    } == original


def test_ensure_pki_regenerates_when_any_file_is_missing(tmp_path):
    db_path = str(tmp_path / "echo.db")
    tls_dir = tmp_path / "tls"
    em_pki.ensure_pki(db_path)
    old_ca = (tls_dir / "ca.pem").read_bytes()
    (tls_dir / "server.key").unlink()

    assert em_pki.ensure_pki(db_path) == str(tls_dir)
    assert (tls_dir / "server.key").exists()
    assert (tls_dir / "ca.pem").read_bytes() != old_ca


def test_ensure_pki_returns_none_without_cryptography(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def reject_cryptography(name, *args, **kwargs):
        if name == "cryptography":
            raise ImportError("synthetic missing dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_cryptography)
    assert em_pki.ensure_pki(str(tmp_path / "db.sqlite")) is None
    assert not (tmp_path / "tls").exists()


def test_server_ssl_context_loads_leaf_and_requires_tls12(tmp_path):
    em_pki._generate(str(tmp_path))

    context = em_pki.server_ssl_context(str(tmp_path))

    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_NONE


def test_ca_pem_returns_ascii_certificate_text(tmp_path):
    em_pki._generate(str(tmp_path))

    value = em_pki.ca_pem(str(tmp_path))

    assert value.startswith("-----BEGIN CERTIFICATE-----\n")
    assert value.endswith("-----END CERTIFICATE-----\n")
    assert x509.load_pem_x509_certificate(value.encode("ascii"))

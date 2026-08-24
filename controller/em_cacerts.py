"""
em_cacerts.py — trusting a private certificate authority.

The media player fetches absolute HTTPS URLs through ffmpeg. A stream served
with a certificate from the user's own internal CA must add that CA to the
container trust store before ffmpeg starts.

WHY THE SYSTEM TRUST STORE, AND NOT SSL_CERT_FILE
-------------------------------------------------
Measured in the built image rather than assumed, because the first version of
this reasoning was wrong.

ffmpeg is built with GnuTLS, which ignores SSL_CERT_FILE and reads the system
bundle. `em_player` explicitly enables `-tls_verify 1` for HTTPS media, so the
system store is the one trust mechanism that applies to every verified decoder
input.

WHY NOT ROUTE THROUGH THE SUPERVISOR PROXY
------------------------------------------
Add-ons can reach Home Assistant at `http://supervisor/core/api/` with
SUPERVISOR_TOKEN, avoiding TLS altogether. It requires `homeassistant_api:
true`, which this project deliberately declined when it would have been
convenient for mirroring user roles (see the add-on notes in CLAUDE.md, and
issue #171): it grants the ENTIRE Home Assistant API. Taking it to avoid
reading one file would be a much larger permission for a smaller benefit, and
it would not help the standalone container at all.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Where update-ca-certificates looks for locally-added anchors on Debian.
ANCHOR_DIR = Path("/usr/local/share/ca-certificates")

_PEM_HEADER = "-----BEGIN CERTIFICATE-----"


def looks_like_pem(text: str) -> bool:
    """
    Whether this is a PEM certificate.

    Checked because update-ca-certificates accepts a DER file, a private key
    or a text file without complaint and simply produces a bundle that does
    not contain the CA — leaving exactly the failure the user was trying to
    fix, with a successful-looking startup in front of it. Better to say so.
    """
    return _PEM_HEADER in text


def anchor_name(src: str | Path) -> str:
    """
    The filename to install as, always ending `.crt`.

    update-ca-certificates processes ONLY files matching `*.crt` in its
    anchor directory. A user pointing at `internal-ca.pem` — the more common
    extension by far — would otherwise have it copied in, skipped silently,
    and nothing would change.
    """
    stem = Path(src).name
    if stem.lower().endswith(".crt"):
        return stem
    return f"{Path(stem).stem}.crt"


class CATrustError(RuntimeError):
    """The certificate could not be trusted, with a reason to show the user."""


def install(src: str, anchor_dir: Path = ANCHOR_DIR, runner=subprocess.run) -> str:
    """
    Install a PEM CA certificate into the system trust store.

    Returns a one-line description of what happened. Raises CATrustError with
    a usable message on anything that would leave the store unchanged —
    startup should fail loudly here rather than run with HTTPS media playback
    that dies with a TLS error nobody connects to this setting.
    """
    path = Path(src)
    if not path.is_file():
        raise CATrustError(
            f"{src} does not exist or is not a file. On the add-on, put the "
            f"certificate in Home Assistant's /ssl directory and set the "
            f"option to /ssl/<filename>; on the container, mount it and give "
            f"the path inside the container."
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        raise CATrustError(f"{src} could not be read: {err}") from err

    if not looks_like_pem(text):
        raise CATrustError(
            f"{src} is not a PEM certificate (no {_PEM_HEADER!r} line). A DER "
            f"or PKCS#12 file has to be converted first: "
            f"openssl x509 -inform der -in ca.der -out ca.crt"
        )

    anchor_dir.mkdir(parents=True, exist_ok=True)
    dest = anchor_dir / anchor_name(src)
    shutil.copyfile(path, dest)

    result = runner(["update-ca-certificates"], capture_output=True, text=True)
    if result.returncode != 0:
        raise CATrustError(
            f"update-ca-certificates failed ({result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )
    return f"trusting {src} as {dest.name}"

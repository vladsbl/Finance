"""Shared TLS helper for the ingestion scripts.

Networks that intercept TLS with a proxy (common in corporate environments)
present a certificate signed by a root CA that lives only in the operating
system's trust store, not in certifi. yfinance/curl_cffi then fail with
``curl: (60) unable to get local issuer certificate``.

``configure_ca_bundle`` merges the OS trust store (Windows ROOT/CA) with
certifi's bundle and exports the environment variables curl_cffi/requests read,
so it must be called *before* importing yfinance. On non-Windows hosts (e.g. a
Linux cron server) it falls back to certifi alone.
"""

import os
import ssl


def configure_ca_bundle(data_dir):
    """Build a CA bundle usable by yfinance/curl_cffi and point SSL env at it.

    Writes ``<data_dir>/ca_bundle.pem`` and sets CURL_CA_BUNDLE,
    SSL_CERT_FILE and REQUESTS_CA_BUNDLE. No-op if certifi is unavailable.
    Must be called before ``import yfinance``.

    Returns the path to the CA bundle (or None if certifi is unavailable), so
    callers using clients that ignore the env vars -- e.g. httpx/groq -- can
    pass ``verify=<bundle>`` explicitly.
    """
    try:
        import certifi
    except ImportError:
        return None

    parts = [open(certifi.where(), "r", encoding="utf-8").read()]
    if hasattr(ssl, "enum_certificates"):  # Windows only
        for store in ("ROOT", "CA"):
            try:
                for cert, _enc, _trust in ssl.enum_certificates(store):
                    try:
                        parts.append(ssl.DER_cert_to_PEM_cert(cert))
                    except Exception:
                        pass
            except Exception:
                pass

    try:
        os.makedirs(data_dir, exist_ok=True)
        bundle = os.path.join(data_dir, "ca_bundle.pem")
        with open(bundle, "w", encoding="utf-8") as fh:
            fh.write("\n".join(parts))
    except OSError:
        bundle = certifi.where()

    for var in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        os.environ[var] = bundle
    return bundle

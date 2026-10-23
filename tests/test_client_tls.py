"""The broker's certificate and host name are verified."""

import os
import socket
import ssl
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.client import QilowattMQTTClient
from tests.test_client_auth import DummyDevice


def test_tls_verifies_certificate_chain_and_host_name():
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=DummyDevice())
    context = client._client._ssl_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert client._client._tls_insecure is False
    # certifi's bundle is loaded rather than relying on the platform store
    assert context.cert_store_stats()["x509_ca"] > 0


def test_tls_can_still_be_turned_off_for_a_local_broker():
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=DummyDevice(), tls=False)
    assert client._client._ssl_context is None


@pytest.fixture
def self_signed_tls_server(tmp_path):
    """A TLS server on localhost presenting a certificate nobody trusts."""
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as err:
        pytest.skip(f"openssl not usable: {err}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    seen = {"handshake_failed": False}

    def serve():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            with context.wrap_socket(conn, server_side=True) as tls:
                tls.recv(1)
        except (ssl.SSLError, OSError):
            seen["handshake_failed"] = True

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], seen
    listener.close()
    thread.join(timeout=5)


def test_untrusted_broker_certificate_is_rejected_before_credentials_are_sent(self_signed_tls_server):
    port, seen = self_signed_tls_server
    client = QilowattMQTTClient(
        mqtt_username="u", mqtt_password="p", device=DummyDevice(), host="localhost", port=port
    )
    with pytest.raises(ssl.SSLCertVerificationError):
        client.connect()
    client.disconnect()
    assert seen["handshake_failed"]   # the server never got past the handshake

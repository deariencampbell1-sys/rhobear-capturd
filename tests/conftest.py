"""Shared fixtures for the capture suites."""
import ipaddress
import socket

import pytest


@pytest.fixture
def _offline_test_dns(monkeypatch):
    """Only reserved test hosts and literal addresses; never a real DNS lookup.

    Opt in per module (`pytestmark = pytest.mark.usefixtures(...)`). The stub
    replaces the resolver capture.py calls; monkeypatch restores it after
    each test.
    """

    def resolve(host, port=None, *args, **kwargs):
        aliases = {"base.test": "93.184.216.34", "example.com": "93.184.216.34",
                   "localhost": "127.0.0.1"}
        try:
            address = ipaddress.ip_address(aliases.get(host, host))
        except ValueError:
            raise socket.gaierror("unconfigured test host") from None
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        sockaddr = (str(address), port or 0, 0, 0) if address.version == 6 else (str(address), port or 0)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    monkeypatch.setattr("capturd.shots.capture.socket.getaddrinfo", resolve)
    return resolve

"""The SDK compatibility tests never reach the network, even on a developer's machine."""

import socket
from typing import Any

import pytest


def _refuse(*args: Any, **kwargs: Any) -> Any:
    raise OSError("the SDK compatibility tests have no network")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # A proxy setting in the environment keeps the client from asking the OS for one,
    # which can crash a forked process on macOS.
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)

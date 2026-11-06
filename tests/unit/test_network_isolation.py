"""Guards the CI claim that make check runs with the network disabled."""

import os
import socket

import pytest


@pytest.mark.skipif(
    os.environ.get("CI_EXPECT_NO_NETWORK") != "1",
    reason="only meaningful inside CI's empty network namespace",
)
def test_network_is_unreachable() -> None:
    with (
        pytest.raises(OSError, match=r"[Uu]nreachable"),
        socket.create_connection(("1.1.1.1", 443), timeout=2),
    ):
        pass

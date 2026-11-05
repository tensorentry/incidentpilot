import urllib.request


def test_real_network_request() -> None:
    with urllib.request.urlopen("https://pypi.org/simple/", timeout=5) as response:  # noqa: S310
        assert response.status == 200

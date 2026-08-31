from timed_sketching_helper.main import _is_loopback


def test_loopback_addresses_are_recognised():
    for host in ("127.0.0.1", "127.0.0.5", "::1", "localhost", ""):
        assert _is_loopback(host) is True


def test_public_addresses_are_not_loopback():
    for host in ("0.0.0.0", "192.168.1.10", "::", "example.com"):
        assert _is_loopback(host) is False

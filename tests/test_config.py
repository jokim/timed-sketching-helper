from timed_sketching_helper.config import load_config


def test_max_images_defaults_to_300(monkeypatch):
    monkeypatch.delenv("MAX_IMAGES", raising=False)
    assert load_config().max_images == 300


def test_max_images_reads_env(monkeypatch):
    monkeypatch.setenv("MAX_IMAGES", "200")
    assert load_config().max_images == 200


def test_max_images_is_hard_capped_at_1000(monkeypatch):
    monkeypatch.setenv("MAX_IMAGES", "5000")
    assert load_config().max_images == 1000


def test_max_requests_defaults_to_100(monkeypatch):
    monkeypatch.delenv("MAX_REQUESTS", raising=False)
    assert load_config().max_requests == 100


def test_max_requests_reads_env(monkeypatch):
    monkeypatch.setenv("MAX_REQUESTS", "400")
    assert load_config().max_requests == 400


def test_max_requests_falls_back_to_100_when_not_numeric(monkeypatch):
    monkeypatch.setenv("MAX_REQUESTS", "lots")
    assert load_config().max_requests == 100


def test_max_requests_is_hard_capped_at_1000(monkeypatch):
    monkeypatch.setenv("MAX_REQUESTS", "50000")
    assert load_config().max_requests == 1000


def test_list_ttl_hours_reads_env(monkeypatch):
    monkeypatch.setenv("LIST_TTL_HOURS", "6")
    assert load_config().list_ttl_hours == 6


def test_list_ttl_hours_falls_back_when_not_numeric(monkeypatch):
    monkeypatch.setenv("LIST_TTL_HOURS", "forever")
    assert load_config().list_ttl_hours == 24


def test_list_ttl_hours_is_floored_at_1(monkeypatch):
    monkeypatch.setenv("LIST_TTL_HOURS", "0")
    assert load_config().list_ttl_hours == 1

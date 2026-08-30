from timed_sketching_helper.config import load_config


def test_max_images_defaults_to_1000(monkeypatch):
    monkeypatch.delenv("MAX_IMAGES", raising=False)
    assert load_config().max_images == 1000


def test_max_images_reads_env(monkeypatch):
    monkeypatch.setenv("MAX_IMAGES", "200")
    assert load_config().max_images == 200


def test_max_images_is_hard_capped_at_1000(monkeypatch):
    monkeypatch.setenv("MAX_IMAGES", "5000")
    assert load_config().max_images == 1000

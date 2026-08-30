import random

from timed_sketching_helper.sessions import build_session


def test_selects_requested_count():
    ids = [f"id{n}" for n in range(50)]

    selected, pool = build_session(ids, 20, rng=random.Random(1))

    assert len(selected) == 20
    assert len(pool) == 30
    assert len(set(selected)) == 20


def test_selected_and_pool_partition_the_input():
    ids = [f"id{n}" for n in range(10)]

    selected, pool = build_session(ids, 4, rng=random.Random(0))

    assert sorted(selected + pool) == sorted(ids)
    assert set(selected).isdisjoint(pool)


def test_count_larger_than_available_returns_all():
    ids = ["a", "b", "c"]

    selected, pool = build_session(ids, 10, rng=random.Random(0))

    assert sorted(selected) == ["a", "b", "c"]
    assert pool == []


def test_is_shuffled_not_input_order():
    ids = [f"id{n:03d}" for n in range(100)]

    selected, _ = build_session(ids, 100, rng=random.Random(42))

    assert selected != ids  # astronomically unlikely to match by chance

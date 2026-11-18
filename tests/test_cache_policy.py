"""Cache policy tests for AsyncTTLCache (SPEC §6, AC-05)."""

from __future__ import annotations

import asyncio
import random

import pytest

from dependency_intelligence.cache import AsyncTTLCache

from .fakes import FakeClock

pytestmark = pytest.mark.cache


def make_cache(
    clock: FakeClock, *, ttl: float = 10, max_entries: int = 2
) -> AsyncTTLCache[str, str]:
    return AsyncTTLCache[str, str](ttl_seconds=ttl, max_entries=max_entries, clock=clock)


async def test_hit_returns_stored_value_and_miss_returns_none() -> None:
    cache = make_cache(FakeClock())
    await cache.put("a", "A")
    assert await cache.get("a") == "A"
    assert await cache.get("missing") is None
    assert await cache.size() == 1


async def test_read_does_not_extend_expiry() -> None:
    clock = FakeClock()
    cache = make_cache(clock)
    await cache.put("a", "A")
    clock.advance(9)
    assert await cache.get("a") == "A"
    clock.advance(1)
    assert await cache.get("a") is None


async def test_entry_is_live_just_before_ttl_and_expired_at_exactly_ttl() -> None:
    clock = FakeClock()
    cache = make_cache(clock)
    await cache.put("a", "A")
    clock.advance(9.5)  # binary-exact steps: 9.5 + 0.5 == 10.0
    assert await cache.get("a") == "A"
    assert await cache.size() == 1
    clock.advance(0.5)
    assert await cache.size() == 0
    assert await cache.get("a") is None


async def test_expired_read_removes_entry() -> None:
    clock = FakeClock()
    cache = make_cache(clock)
    await cache.put("a", "A")
    clock.advance(10)
    assert await cache.get("a") is None
    # Re-inserting after expiry starts a fresh lifetime.
    await cache.put("a", "A2")
    assert await cache.get("a") == "A2"


async def test_expired_entries_are_purged_before_lru_eviction() -> None:
    clock = FakeClock()
    cache = make_cache(clock, max_entries=2)
    await cache.put("old", "OLD")  # expires at 10
    clock.advance(5)
    await cache.put("lru", "LRU")  # expires at 15
    assert await cache.get("old") == "OLD"  # "old" becomes MRU, so "lru" is the LRU entry
    clock.advance(5)  # now 10: "old" is expired but still occupies a slot
    await cache.put("new", "NEW")
    assert await cache.get("lru") == "LRU"
    assert await cache.get("new") == "NEW"
    assert await cache.size() == 2


async def test_lru_eviction_when_all_entries_live() -> None:
    cache = make_cache(FakeClock(), max_entries=3)
    for key in ("a", "b", "c"):
        await cache.put(key, key.upper())
    assert await cache.get("a") == "A"  # recency: b, c, a
    await cache.put("d", "D")
    assert await cache.get("b") is None
    assert await cache.get("a") == "A"
    assert await cache.get("c") == "C"
    assert await cache.get("d") == "D"


async def test_max_entries_one_keeps_only_latest() -> None:
    cache = make_cache(FakeClock(), max_entries=1)
    await cache.put("a", "A")
    assert await cache.get("a") == "A"
    await cache.put("b", "B")
    assert await cache.get("a") is None
    assert await cache.get("b") == "B"
    assert await cache.size() == 1
    await cache.put("b", "B2")
    assert await cache.get("b") == "B2"
    assert await cache.size() == 1


async def test_reput_replaces_value_and_resets_ttl() -> None:
    clock = FakeClock()
    cache = make_cache(clock)
    await cache.put("a", "A")
    clock.advance(6)
    await cache.put("a", "A2")  # expires at 16, not 10
    clock.advance(6)  # now 12
    assert await cache.get("a") == "A2"
    clock.advance(4)  # now 16
    assert await cache.get("a") is None


async def test_reput_marks_key_most_recently_used_without_eviction() -> None:
    cache = make_cache(FakeClock(), max_entries=2)
    await cache.put("a", "A")
    await cache.put("b", "B")
    await cache.put("a", "A2")  # existing key: no eviction, recency: b, a
    assert await cache.size() == 2
    await cache.put("c", "C")
    assert await cache.get("b") is None
    assert await cache.get("a") == "A2"
    assert await cache.get("c") == "C"


async def test_concurrent_get_put_never_exceeds_capacity_or_raises() -> None:
    """Smoke test: many interleaved tasks keep the size bound and never raise.

    The cache's critical sections contain no ``await``, so tasks cannot be
    suspended mid-update; this checks the observable invariants under heavy
    interleaving rather than forcing lock contention.
    """
    clock = FakeClock()
    max_entries = 8
    cache = make_cache(clock, ttl=5, max_entries=max_entries)
    rng = random.Random(1234)
    keys = [f"k{i}" for i in range(32)]
    observed_sizes: list[int] = []

    async def worker(seed: int) -> None:
        local = random.Random(seed)
        for _ in range(200):
            key = local.choice(keys)
            if local.random() < 0.5:
                await cache.put(key, key.upper())
            else:
                value = await cache.get(key)
                assert value is None or value == key.upper()
            if local.random() < 0.05:
                clock.advance(local.random())
            if local.random() < 0.2:
                observed_sizes.append(await cache.size())
            await asyncio.sleep(0)

    await asyncio.gather(*(worker(rng.randrange(1 << 30)) for _ in range(50)))
    observed_sizes.append(await cache.size())
    assert observed_sizes
    assert max(observed_sizes) <= max_entries


@pytest.mark.parametrize("ttl", [0, -1, -0.5])
def test_constructor_rejects_non_positive_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        AsyncTTLCache[str, str](ttl_seconds=ttl, max_entries=1)


@pytest.mark.parametrize("max_entries", [0, -1])
def test_constructor_rejects_non_positive_max_entries(max_entries: int) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        AsyncTTLCache[str, str](ttl_seconds=1, max_entries=max_entries)

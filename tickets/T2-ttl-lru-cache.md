# T2: Async TTL/LRU cache

Owner: B. Commit: `Implement TTL and LRU assessment cache`.
General rules: [README.md](README.md). Spec: `SPEC.md` §6; AC-05 (expiry and eviction).

## Goal

Implement `AsyncTTLCache.get`, `put`, and `size` as a concurrency-safe,
process-local cache with a monotonic clock, insertion-time expiry, and LRU
eviction.

## May modify

- `src/dependency_intelligence/cache.py`
- new: `tests/test_cache_policy.py` (mark every test `@pytest.mark.cache`)

## Read-only context

`ports.py` (`AssessmentCache` protocol; `AsyncTTLCache[str, PackageAssessment]`
must satisfy it), `domain.py` (`DependencyCoordinate.cache_key`),
`tests/fakes.py` (`FakeClock`), `tests/test_contract.py`
(`test_cache_expires_and_evicts_lru`), `bootstrap.py` (how it is constructed).

## Requirements (§6)

- Constructor signature and validation stay exactly as supplied
  (`ttl_seconds`, `max_entries`, `clock`; non-positive values raise `ValueError`).
- Time comes only from the injected `clock` (default `time.monotonic`).
- Expiry is fixed at insertion: `expires_at = clock() + ttl_seconds`. A read
  never extends it.
- A read of a live entry marks it most recently used. A read of an expired entry
  returns `None` and removes it.
- When an insert exceeds `max_entries`, expired entries are dropped first, then
  the least recently used live entry is evicted. Live entries must never be
  evicted while expired ones still occupy slots.
- `size()` returns the number of live entries after removing expired ones.
- Safe under concurrent `get`/`put` from many tasks (use `asyncio.Lock`, or keep
  critical sections free of `await` and document why that suffices).
- Generic over key and value; the cache does not inspect values and does not
  enforce "complete only" (that is T3's rule).

## Acceptance criteria

- Contract test passes: `a`,`b` inserted, `a` read, `c` inserted evicts `b`; at
  `ttl` seconds `a` and `c` are gone and `size() == 0`.
- New tests cover:
  - a read does not extend expiry (entry still expires at insertion + ttl);
  - expiry boundary at exactly `ttl`;
  - expired entries are purged before LRU eviction;
  - `max_entries=1` behavior;
  - re-`put` of an existing key (see decision below);
  - many concurrent `get`/`put` tasks never exceed `max_entries` and never raise;
  - constructor rejects `ttl_seconds <= 0` and `max_entries <= 0`.
- Tests use `FakeClock`; no real sleeping.

## Watch-outs

- Use `collections.OrderedDict` (or equivalent) for O(1) recency updates.
- Keep strict typing: no `Any` leaking into the public signatures.
- Do not add a background sweeper task: nothing may keep running after the cache
  is dropped.

## Focused check

```bash
uv run pytest -m cache
uv run ruff check src/dependency_intelligence/cache.py tests/test_cache_policy.py
uv run ruff format --check src/dependency_intelligence/cache.py tests/test_cache_policy.py
uv run mypy src tests checks
```

## Evidence to return

Diff summary, the command output above, unresolved risk, and the decisions below.

## Decisions to hand to the integrator

- Whether re-`put` of an existing key resets both its TTL and its recency.
- Expiry boundary is `now >= inserted_at + ttl` (the contract test requires
  expiry at exactly `ttl`).

## Do not

Edit `ports.py`, `tests/fakes.py`, `service.py`, `bootstrap.py`, or any other
ticket's file. Do not change the constructor or add public methods.

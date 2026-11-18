# T3: Scanner orchestration

Owner: C. Commit: `Implement concurrent bounded dependency scanner`.
General rules: [README.md](README.md). Spec: `SPEC.md` §2, §5, §6, §7, §8; AC-03 to AC-08.

## Goal

Implement `DependencyScanner.scan` and `DependencyScanner.assess`: concurrent
sources, bounded package concurrency, stable order, cancellation, cache and
single-flight, and attributed partial failure.

## May modify

- `src/dependency_intelligence/service.py`
- new: `tests/test_scanner_orchestration.py` (mark every test `@pytest.mark.orchestration`)

## Read-only context

`ports.py`, `domain.py` (`DependencyScanResult`, `PackageAssessment`,
`ProjectScanReport.status`, `CacheStatus`), `errors.py` (`SourceRequestError`),
`singleflight.py` (`SingleFlight.run` returns `(value, shared)`),
`tests/fakes.py` (`StartGate`, fake sources, `MemoryCache`),
`tests/test_contract.py` (all `orchestration` tests), `reporting.py`.

## Requirements

### `assess(coordinate) -> DependencyScanResult`

1. Look up `coordinate.cache_key`. A hit returns the cached assessment with
   `CacheStatus.HIT` and no failures, with zero source calls.
2. A miss runs through `SingleFlight` keyed by `cache_key`:
   - the first caller reports `MISS`; every other concurrent caller reports `COALESCED`;
   - exactly one source load happens for identical concurrent misses.
3. The load starts the PyPI, OSV, and deps.dev calls together (AC-03); none waits
   for another to finish.
4. One source failing must not cancel its siblings. Each call is converted to a
   value or an attributed `SourceFailure`. Do not use a bare `TaskGroup`, which
   cancels siblings on the first exception.
5. Build `PackageAssessment` with `None` for each failed source. Failures are
   ordered deterministically (PyPI, OSV, deps.dev).
6. If all three sources fail, return `assessment=None` with all three failures.
   `DependencyScanResult.useful` is `assessment is not None`; a hollow assessment
   would turn a `failed` project into `partial`.
7. Put into the cache only a complete assessment, from inside the flight work so
   no window exists between flight removal and the cache write. Partial and
   failed results are never cached, so the next scan re-calls the missing source.
8. Non-`SourceRequestError` exceptions become `FailureKind.UNEXPECTED`,
   attributed to the source's `service_name`, with a safe fixed detail (never
   `str(error)` or a response body). `asyncio.CancelledError` is never swallowed.

### `scan(dependencies) -> ProjectScanReport`

- At most `max_concurrent_packages` packages are assessed at once (default 8;
  constructor already rejects non-positive values).
- `results` order equals input order regardless of completion order.
- Memory is bounded: do not hold more in-flight work than necessary.
- Cancelling `scan` cancels and awaits all outstanding child tasks before
  re-raising.

### Cancellation (§5)

- Cancelling one waiter of a shared load must not cancel work another waiter
  still needs (provided by `SingleFlight`).
- When the final waiter leaves, outstanding source work is cancelled, and the
  load must propagate that cancellation to its three source tasks.

## Acceptance criteria

- Existing contract tests: `uv run pytest -m orchestration` passes.
- New tests cover:
  - all-three-failed gives `assessment is None`, three attributed failures,
    `ProjectScanReport.status is FAILED`;
  - each single-source failure yields `PARTIAL` with the other fields intact and
    the failure attributed to the right `ServiceName`;
  - `None` vs `()` for vulnerabilities: a successful empty OSV answer is `()` and
    the result is complete and cacheable;
  - a plain `Exception` from a source is `UNEXPECTED`, attributed, with a safe detail;
  - the package limit never exceeds N concurrent assessments over many packages;
  - `scan` cancellation cancels every in-flight source gate and awaits them;
  - cancelling one of two coalesced waiters leaves the load running;
  - partial results are not stored in the cache (use `MemoryCache.entries`).

## Dependencies and merge note

- Develops against the frozen ports and `tests/fakes.py`; no other ticket's code
  is needed.
- `test_complete_result_is_cached_and_identical_misses_coalesce` uses the real
  `AsyncTTLCache`, so it passes only after T2 is merged.

## Watch-outs

- `test_cancelling_final_waiter_cancels_sources` allows only 5 event-loop turns
  for all three gates to observe cancellation. Check your cancellation structure
  against it.
- Do not edit `singleflight.py`; it cancels the flight task but does not await
  it, so the load itself must clean up its children.
- Keep I/O out of `service.py`: it talks only to the port protocols.

## Focused check

```bash
# before T2 merges
uv run pytest -m orchestration -k "not complete_result_is_cached"
# after T2 merges
uv run pytest -m orchestration
uv run ruff check src/dependency_intelligence/service.py tests/test_scanner_orchestration.py
uv run ruff format --check src/dependency_intelligence/service.py tests/test_scanner_orchestration.py
uv run mypy src tests checks
```

## Evidence to return

Diff summary, command output, unresolved risk, and the decisions below.

## Decisions to hand to the integrator

- Where the cache write happens (inside the flight work) and why.
- The cache status that coalesced waiters of a partial result report, and
  that the same failures are shared with them.
- Mapping of unexpected exceptions to `UNEXPECTED` with a fixed safe detail.
- All-sources-failed gives `assessment=None`.

## Do not

Edit `singleflight.py`, `ports.py`, `domain.py`, `reporting.py`, `cache.py`,
`tests/fakes.py`, or any other ticket's file.

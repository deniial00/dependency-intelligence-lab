# Engineering decisions

Record only decisions that affect behavior, interfaces, failure semantics, or
verification. “The AI suggested it” is not a rationale.

## Decision 1

- Context: SPEC 4.3 requires a deps.dev graph with exactly one `SELF` root and no
  reported errors. It does not say whether the `versionKey` in the metadata or in
  the root node must equal the requested coordinate. The supplied
  `VersionKey.coordinate()` helper exists but nothing in the specification uses it.
- Options considered: (a) reject a metadata or root `versionKey` that differs from the
  request as `invalid_response`; (b) validate only the structure SPEC 4.3 lists.
- Decision: (b). `DepsDevClient` does not compare `versionKey` values with the
  requested coordinate and does not call `VersionKey.coordinate()`.
- Consequences and risk: the request URL already pins the exact coordinate. Option (a)
  would reject responses for reasons the specification does not give, and upstream
  spellings can legitimately differ (name casing, `1.0` versus `1.0.0`). The cost is
  that data for a different coordinate, returned by a faulty proxy or upstream, is
  accepted. Revisit if the specification adds an identity requirement.
- Specification or acceptance criteria affected: SPEC 4.3, AC-02.
- Evidence that will verify it: `tests/test_sources_adapters.py`
  `test_deps_dev_does_not_compare_version_keys_with_the_requested_coordinate`, and
  `tests/test_contract.py` `test_source_adapters_translate_valid_responses`.

## Decision 2

- Context: the two deps.dev requests (metadata and `:dependencies`) run together, and
  SPEC 5 asks that outstanding work is cancelled when it is no longer needed. Either
  request can fail. If both fail, the specification does not say which failure the
  caller sees.
- Options considered: (a) fail fast: the first failure cancels the other request and is
  reported; (b) wait for both requests to settle, then report the metadata failure.
- Decision: (a). The first failure in time is reported as the original
  `SourceRequestError`, never an `ExceptionGroup`. A request cancelled before it could
  fail reports nothing. Failures that land in the same event-loop pass are ordered by
  endpoint, metadata first, so a tie does not depend on callback order.
- Consequences and risk: which failure kind is reported when both endpoints are
  failing can depend on timing. The scanner reports one deps.dev failure either way.
  Option (b) would be fully deterministic, but a slow or hung sibling would hold a
  pooled connection and delay the report until it timed out.
- Specification or acceptance criteria affected: SPEC 4.3 and 5, AC-02, AC-03.
- Evidence that will verify it: `tests/test_sources_adapters.py`
  `test_deps_dev_reports_the_first_failure_and_cancels_a_later_one`,
  `test_deps_dev_failures_in_the_same_pass_are_ordered_by_endpoint`,
  `test_deps_dev_failure_cancels_the_other_request_and_is_not_grouped`, and
  `test_cancelling_deps_dev_cancels_both_requests`.

## Decision 3

- Context: SPEC 4.2 asks for one `POST /v1/query` with the exact coordinate. The OSV API
  can answer a large result set with a `next_page_token`, which a client may send back
  to fetch the next page.
- Options considered: (a) follow `next_page_token` until it is absent; (b) make the
  single query the specification describes and ignore the token.
- Decision: (b). `OSVClient` sends one request per coordinate and does not follow
  `next_page_token`.
- Consequences and risk: if OSV ever paginates a coordinate's advisories, the result
  lists only the first page and is reported as a complete, cacheable answer, with no
  failure recorded. This was not observed with the supplied fixtures. Following
  pagination would add requests per package, which interacts with the bounded
  connection pool and request timeout, and needs a specified page limit.
- Specification or acceptance criteria affected: SPEC 4.2, AC-02.
- Evidence that will verify it: `tests/test_sources_adapters.py`
  `test_osv_makes_a_single_query_and_ignores_next_page_token`.

## Decision 4

- Context: SPEC 6 fixes expiry at insertion and bounds the cache with LRU eviction, but
  does not say what a second `put` for a key that is already cached does to that
  entry's expiry or recency. T3 re-puts a key whenever a fresh complete assessment
  replaces one, for example after the earlier entry expired.
- Options considered: (a) replace the value and reset both its expiry and its recency;
  (b) replace the value but keep the original expiry; (c) ignore the second `put`
  while the existing entry is live.
- Decision: (a). `AsyncTTLCache.put` on an existing key stores the new value, sets
  `expires_at = clock() + ttl_seconds`, and moves the key to the most recently used
  position. Replacing a key never evicts another entry, because the entry count does
  not grow.
- Consequences and risk: a `put` is a fresh insertion, so "expiry fixed at insertion"
  still holds and a read never extends expiry. A key that is re-put repeatedly can stay
  cached longer than one TTL from its first insertion, but each stored value is never
  older than one TTL. Option (b) would let fresh data expire on the old schedule.
  Option (c) would keep serving the older value when the caller has newer data.
- Specification or acceptance criteria affected: SPEC 6, AC-05.
- Evidence that will verify it: `tests/test_cache_policy.py`
  `test_reput_replaces_value_and_resets_ttl`,
  `test_reput_marks_key_most_recently_used_without_eviction`, and
  `test_max_entries_one_keeps_only_latest`.

## Decision 5

- Context: SPEC 6 says entries expire a fixed TTL after insertion, but does not say
  whether an entry is still live at exactly `inserted_at + ttl`. The contract test
  advances the clock by exactly `ttl` and expects the entries to be gone.
- Options considered: (a) inclusive boundary: expired when `now >= inserted_at + ttl`;
  (b) exclusive boundary: expired only when `now > inserted_at + ttl`.
- Decision: (a). An entry is expired once `clock() >= expires_at`. The rule lives in one
  place (`_Entry.is_expired`) and is shared by `get`, `size`, and eviction.
- Consequences and risk: an entry is live for exactly `ttl` seconds on the injected
  monotonic clock, and no longer. Option (b) would fail
  `test_cache_expires_and_evicts_lru`. With real clocks the difference is negligible.
- Specification or acceptance criteria affected: SPEC 6, AC-05.
- Evidence that will verify it: `tests/test_contract.py`
  `test_cache_expires_and_evicts_lru`, and `tests/test_cache_policy.py`
  `test_entry_is_live_just_before_ttl_and_expired_at_exactly_ttl` and
  `test_read_does_not_extend_expiry`.

## Decision 6

- Context: SPEC 6 requires a concurrency-safe cache. T2 allows either an
  `asyncio.Lock` or critical sections that contain no `await`. The choice changes what
  the concurrency test can prove.
- Options considered: (a) rely only on having no `await` inside critical sections;
  (b) guard every read and write of the cache's state with one `asyncio.Lock`, keeping
  the critical sections free of `await` as well.
- Decision: (b). `get`, `put`, and `size` each take the lock. Expired entries are purged
  only when an insert goes over capacity or `size()` is called, with no background
  task, so nothing keeps running after the cache is dropped.
- Consequences and risk: correctness does not depend on a future edit keeping the
  critical sections free of `await`. Because tasks are never suspended inside a
  critical section today, they never contend for the lock, so the concurrency test is
  smoke evidence (the size bound holds and nothing raises), not proof that the lock is
  needed. A purge scans every entry, which is fine at the default 2,000 entries but
  makes `size()` cost grow with the cache.
- Specification or acceptance criteria affected: SPEC 6, AC-05.
- Evidence that will verify it: `tests/test_cache_policy.py`
  `test_concurrent_get_put_never_exceeds_capacity_or_raises` and
  `test_expired_entries_are_purged_before_lru_eviction`.

## Decision 7

- Context: SPEC 5 and 6 say only complete assessments are cached and that concurrent
  misses share one load. They do not say where the cache write happens relative to the
  shared load, what coalesced waiters report when the shared result is partial, or how a
  non-source exception, or a package whose three sources all failed, is represented.
- Options considered: write the cache after the shared load returns, in `assess`, or
  inside the load; map an unexpected exception to a failure, or let it escape; represent
  an all-failed package as an assessment of three `None` fields, or as no assessment.
- Decision: (1) `DependencyScanner._load` writes the cache itself, before the shared load
  completes, and only for a complete assessment. (2) Waiters coalesced onto a partial load
  report `coalesced` with the same failures as the first caller, and nothing is cached.
  (3) Any exception other than `SourceRequestError` or cancellation becomes
  `FailureKind.UNEXPECTED`, attributed to its source, not retryable, with the fixed detail
  "unexpected source error" (never `str(error)`); cancellation always propagates.
  (4) When all three sources fail, the result has `assessment=None`. Failures are ordered
  PyPI, OSV, deps.dev.
- Consequences and risk: no window exists between the load leaving the flight table and the
  cache write, so a later caller cannot start a duplicate load. `DependencyScanResult.useful`
  is `assessment is not None`, so a hollow assessment would turn a failed project into
  `partial` and HTTP 503 into 200. Known gap: `scan()` cancels its workers only when it is
  cancelled. If `assess` raises anything else, for example a failing cache backend, `scan`
  raises while the sibling workers and their source calls keep running. An integration probe
  with a cache that raised on one package left 5 tasks running and cancelled 0 of 3 source
  calls. The shipped `AsyncTTLCache` never raises, so this is not reachable in the supplied
  composition. It needs a follow-up in `service.py`, which is outside T4's files.
- Specification or acceptance criteria affected: SPEC 5, 6, 7; AC-04, AC-05, AC-06, AC-07.
- Evidence that will verify it: `tests/test_scanner_orchestration.py`
  `test_all_sources_failed_returns_failed_report`, `test_single_source_failure_is_partial`,
  `test_partial_result_is_not_cached`, `test_plain_exception_is_unexpected_and_safe`,
  `test_cancelling_one_coalesced_waiter_keeps_load_running`, and
  `tests/test_contract.py` `test_complete_result_is_cached_and_identical_misses_coalesce`.
  Three concurrent callers with OSV failing gave `miss`, `coalesced`, `coalesced`, one
  shared failure, 0 cached entries and 1 OSV call in a one-off integration run; no test
  pins that case.

## Decision 8

- Context: SPEC 3 accepts only the `PyPI` ecosystem. `DependencyCoordinate` compares
  `ecosystem.casefold()` with `pypi` and stores `PyPI`, so the HTTP input
  `"ecosystem": "pypi"` or `"PYPI"` is accepted.
- Options considered: (a) accept any casing and store `PyPI`; (b) require the exact spelling
  `PyPI` in the transport schema, which means changing the supplied `api.py` or `domain.py`.
- Decision: (a). Any other ecosystem is rejected with HTTP 422 before scanning, and the
  reported and cached ecosystem is always `PyPI`.
- Consequences and risk: a client that sends `pypi` succeeds where the strictest reading
  would reject it. Nothing downstream differs, because the cache key and every report use
  the normalized value. Tighten the schema if the acceptance rules require exact casing.
- Specification or acceptance criteria affected: SPEC 3.1, AC-01.
- Evidence that will verify it: `tests/test_transport_boundaries.py`
  `test_lowercase_ecosystem_is_accepted_and_normalized` and the `non-pypi-ecosystem` case
  of `test_invalid_request_is_rejected_before_any_scan`.

## Decision 9

- Context: SPEC 5 requires the shared HTTP pool to stay bounded by configuration. The API
  lifespan built its client with `httpx.Limits(max_connections=settings.max_http_connections)`.
  `tool_cli.py` built its own `httpx.AsyncClient(timeout=...)` without limits, so
  `MAX_HTTP_CONNECTIONS` was ignored on the agent-tool path and httpx's default of 100
  applied, with no `User-Agent`. Reproduced before the fix: six packages and 24 requests
  opened 24 connections against a configured limit of 2.
- Options considered: (a) repeat the limits in `tool_cli.py`; (b) one
  `build_http_client(settings)` in `bootstrap.py` used by both entry points; (c) make
  `build_scanner` own the client and change its signature.
- Decision: (b). Timeout, connection limits, and `User-Agent` come from `Settings` in one
  place, and `max_keepalive_connections` equals `max_connections`. `build_scanner` keeps its
  signature.
- Consequences and risk: both transports now share one client policy, and the command line
  tool sends the same `User-Agent` as the API. The bound is per client, not per upstream
  host, so one slow host can occupy the whole pool.
- Specification or acceptance criteria affected: SPEC 5.
- Evidence that will verify it: `tests/test_transport_boundaries.py`
  `test_api_keeps_the_shared_http_pool_bounded` and
  `test_agent_tool_keeps_the_shared_http_pool_bounded` (24 requests, peak open connections
  equal to the configured 2). The tool test fails with `24 == 2` when `tool_cli.py` builds
  its own client.

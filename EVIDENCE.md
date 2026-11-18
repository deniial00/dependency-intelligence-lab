# Verification evidence and acceptance decision

Runtime evidence was collected on branch `t4-transport-integration` (code identical to
`c58f5ac`) with `dependency-fake-upstreams` on :8090 and `dependency-api` on :8000. Both were
stopped afterwards and the ports checked. No credentials are involved.

## Quality gate

| Command | Exit | Concise result |
| --- | ---: | --- |
| `uv run ruff check .` | 0 | All checks passed |
| `uv run ruff format --check .` | 0 | 41 files already formatted (see note) |
| `uv run mypy src tests checks` | 0 | Success: no issues found in 30 source files |
| `uv run pytest` | 0 | 163 passed in 1.85s |

Per marker: `sources` 64, `cache` 16, `orchestration` 14, `transport` 65, all passed.

Note: `ruff format --check .` exits 1 in a working tree that also holds the untracked
`tickets/REVIEW-T2-T3.md`, because that file has a Python block ruff reformats. The row above is
the result with `--exclude tickets/REVIEW-T2-T3.md`. That file is not part of the T4 commits; it
must be formatted or removed before it is committed, or AC-10 fails.

## Concurrency evidence

Command or test:

- Deterministic: `uv run pytest tests/test_contract.py -k "three_sources_start or deps_dev_requests_start"`.
- Runtime: one `POST /v1/scans` with `slow-a`, `slow-b`, `slow-c` (all `1.0`). The fixture sleeps
  2 s inside every upstream call.

Observed timing/start-order evidence:

- The two tests pass. `StartGate` records every start while its release event is still unset, so all
  three sources (and both deps.dev calls) have started before any may finish.
- Runtime: HTTP 200, `complete`, `time_total=2.03s`. The fixture counted 12 upstream calls:
  `{"pypi":3,"osv":3,"deps":3,"deps_graph":3}`.

Why this proves overlap rather than only fast execution:

- Twelve calls of 2 s each take at least 24 s one after another. The scan took 2.03 s, about one
  call's sleep, so the calls ran at the same time, across sources and across packages.
- The gate tests need no timing at all: they fail if any source waits for another to finish.

## Cache evidence

Run the same API request twice, then inspect `GET http://127.0.0.1:8090/_admin/calls`.

First response cache state: `jinja2==2.4.1` and `fastapi==0.100.0`: HTTP 200, `complete`,
`["miss", "miss"]`.

Second response cache state: same body: HTTP 200, `complete`, `["hit", "hit"]`, `time_total=0.0014s`.

Upstream counters before/after the second request:

```text
before: {"pypi":2,"osv":2,"deps":2,"deps_graph":2}
after:  {"pypi":2,"osv":2,"deps":2,"deps_graph":2}
```

Single-flight: eight parallel identical requests for one fresh slow coordinate gave
`{'miss': 1, 'coalesced': 7}` and exactly one call per upstream endpoint
(`{"osv":1,"deps":1,"deps_graph":1,"pypi":1}`).

## Partial-failure evidence

Scan `unavailable-osv-demo==1.0.0` against the fixtures.

Successful fields retained: HTTP 200, status `partial`, `latest_version` `2.0.0`,
`licenses` `["MIT"]`, `direct_dependency_count` 1, `published_at` `2024-09-17T19:18:10Z`,
`complete` false.

Attributed failure:

```json
{"dependency": "unavailable-osv-demo==1.0.0", "service": "osv", "kind": "unavailable",
 "retryable": true, "detail": "service returned HTTP 503", "status_code": 503}
```

The fixture saw two OSV calls for this one scan (the supplied HTTP policy retries once). Repeating
the scan reported `miss` again and the counters rose to `{"osv":4,"deps":2,"deps_graph":2,"pypi":2}`,
so the partial result was not cached.

Why `vulnerabilities: null` is different from `vulnerabilities: []`:

- `[]` means OSV answered and listed no advisory (`requests==2.0`). `null` means OSV did not answer,
  so nothing is known. In one scan of `jinja2==2.4.1`, `requests==2.0` and the unavailable package,
  the values were a one-item list, `[]` and `null`.
- `vulnerable_dependencies` counted 1 of 3. The package with `null` is not counted and not claimed safe.

## Agent-tool boundary evidence

Attempt `../requirements.lock` through the tool adapter.

```text
$ uv run dependency-agent-tool --workspace <tmp>/workspace --manifest ../requirements.lock
{"status": "invalid_manifest_path", "message": "hidden and parent path components are not allowed"}
exit 2
```

`<tmp>/requirements.lock` held `secret==1.0`. An absolute path to that file was rejected too
(`manifest path must be relative to the workspace`). A valid manifest in the workspace returned
`partial` with the API's report shape.

Observed result: rejected before any file read or scan.

Boundary being enforced: the manifest is named by a workspace-relative path with no absolute,
hidden, or `..` components and the exact filename `requirements.lock`. After symlinks are resolved
it must still lie inside the workspace and be a regular file. The tests that use a scanner that must
not be called cover absolute, parent, hidden, other filename, escaping symlinked file, escaping
symlinked directory, directory, and missing file. With the symlink containment check removed, the
two escaping-symlink tests fail; they pass with it restored.

## Specification trace

| Acceptance criterion | Implementing commit(s) | Test or runtime evidence |
| --- | --- | --- |
| AC-01 | Starter `api.py`, `manifest.py`, `agent_tool.py` (02bf073); tests in c58f5ac | `test_transport_boundaries.py`: `test_invalid_request_is_rejected_before_any_scan` (16 bodies), `test_malformed_json_is_rejected_before_any_scan`, `test_tool_reports_unsupported_manifest_lines_with_line_numbers` (12), `test_tool_rejects_manifests_over_the_limits`, `test_tool_accepts_manifests_at_the_limits` |
| AC-02 | 22be499, d7b415b, 4145d60 | `test_contract.py::test_source_adapters_translate_valid_responses`; `test_sources_adapters.py` malformed-response and failure-attribution cases; fixture run of all three adapters |
| AC-03 | a06141f (three sources), 22be499 (deps.dev) | `test_contract.py::test_three_sources_start_concurrently`, `test_deps_dev_requests_start_concurrently`; runtime: 12 calls of 2 s in 2.03 s |
| AC-04 | a06141f | `test_contract.py::test_package_limit_and_order_are_preserved`; `test_scanner_orchestration.py::test_package_limit_and_order`. No runtime run of the package limit |
| AC-05 | ffa74f2, 45b6b06 (cache); a06141f (service) | `test_contract.py::test_cache_expires_and_evicts_lru`, `test_complete_result_is_cached_and_identical_misses_coalesce`; `test_cache_policy.py`; runtime: `hit, hit` with unchanged counters, and 1 miss + 7 coalesced |
| AC-06 | a06141f, 22be499 | `test_contract.py::test_partial_result_is_attributed_and_not_cached`; `test_scanner_orchestration.py::test_single_source_failure_is_partial`; runtime: partial run above |
| AC-07 | a06141f; HTTP mapping in starter `api.py`, tests in c58f5ac | `test_transport_boundaries.py::test_report_status_maps_to_http_status`, `test_failed_report_names_every_failed_source_and_keeps_unknowns_null`; runtime: `missing-demo==1.0.0` gave HTTP 503, `failed`, failures `pypi`, `osv`, `deps.dev` in that order, all `not_found` |
| AC-08 | Starter `api.py`; a06141f; tests in c58f5ac | `test_transport_boundaries.py::test_deadline_returns_504_and_cancels_the_scanner`, `test_deadline_cancels_outstanding_source_calls_of_the_real_scanner`, `test_tool_deadline_returns_scan_timeout_and_cancels_the_scanner`; runtime: `SCAN_TIMEOUT_SECONDS=1` and `slow-demo==1.0.0` gave HTTP 504 `scan_timeout` after 1.008 s and no traceback in the API log |
| AC-09 | Starter `agent_tool.py`, `manifest.py`; c58f5ac (tests, shared client factory) | `test_transport_boundaries.py::test_tool_rejects_paths_outside_the_boundary_without_scanning` (10), `test_tool_rejects_an_absolute_path_without_scanning`, `test_tool_returns_the_api_report_shape_for_a_valid_manifest`, `test_tool_never_writes_to_the_workspace`; runtime: `../requirements.lock` rejected |
| AC-10 | All of the above; this commit | Gate table above. Concerns sit in separate modules: `sources/` (I/O adapters), `service.py` (orchestration), `cache.py`, `manifest.py` (parsing), `reporting.py` (presentation), `api.py` and `agent_tool.py` (transport). Known gaps are listed below |

## Human acceptance decision

Draft written by the integrator from the evidence above. The human owner must confirm or change it.

- Decision: accept with stated limitation
- Evidence supporting the decision: the gate passes with 163 tests. Every acceptance criterion has a
  named test, and AC-03, 05, 06, 07, 08 and 09 also ran against the fixtures. Two mutation checks
  failed the tests as they should: the escape check removed from `agent_tool.py`, and `tool_cli.py`
  built without connection limits (24 connections opened against a limit of 2, now 2). The shared
  client factory (Decision 9) closes that second gap.
- Unresolved production risk:
  1. `DependencyScanner.scan` leaves tasks running if `assess` raises anything but a cancellation.
     Reproduced with a cache that raised on one package: 5 tasks stayed alive and 0 of 3 source calls
     were cancelled. The shipped `AsyncTTLCache` never raises, so it is unreachable today. The fix
     is in `service.py` (T3 follow-up: cancel and await workers on any exception, plus a test).
  2. The connection-pool risk in the T4 ticket was tested and not reproduced. With every upstream
     call taking 2 s (single run each): defaults (8 packages, 32 calls, 24 connections) completed in
     4.1 s; a 3 s timeout with 24, 8 and 4 connections completed in 4.0 s, 8.0 s and 16.1 s with
     no failures (the 4-connection run also showed exactly 32 upstream calls, so no request was
     resent). In plain `httpx` (pool of 2, timeout 3 s, six 2 s
     requests) the last pair waited 4 s for a connection and succeeded, while a 0.5 s pool timeout
     did raise `PoolTimeout`. So a queued request fails when the first free connection takes longer
     than the timeout, and otherwise it keeps waiting. [INFERENCE] The wait timer seems to restart
     when a freed connection is reassigned; I did not read `httpcore` to confirm this. It is
     observed behaviour of httpx 0.28.1 and httpcore 1.0.9, not a documented guarantee, so recheck it
     when they are upgraded. What remains: a pool smaller than packages x 4 makes a scan take extra
     round trips, bounded only by `scan_timeout_seconds`, and the bound is per client, not per host.
  3. OSV `next_page_token` is not followed (Decision 3), so a paginated advisory list would look
     complete.
  4. After a cancelled `scan` returns, one `SingleFlight` task can still be unwinding for one
     event-loop turn. `singleflight.py` is supplied and unchanged.
- Why passing tests alone are or are not sufficient here: they are not sufficient. Before T4 the whole
  suite was green while the agent-tool path ignored `MAX_HTTP_CONNECTIONS`, because no test used a
  real pooled client. The `scan` task leak needs a failing cache, which no supplied test uses. And the
  cache counters, the overlap timing, and the pool behaviour showed up only against a running server.

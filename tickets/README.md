# Ticket plan: concurrent dependency intelligence

Source of truth: `SPEC.md` (never edited). Working rules: `AGENTS.md`.
This directory splits the implementation into four tickets with disjoint file
ownership. Each ticket file is self-contained and can be handed to one owner (or
one harness session) as-is.

## Tickets

| Ticket | Goal | Owner | Spec / AC | Marker |
| --- | --- | --- | --- | --- |
| [T1](T1-source-adapters.md) | PyPI, OSV, deps.dev adapters | A | §4, AC-02, AC-03 (deps.dev overlap) | `sources` |
| [T2](T2-ttl-lru-cache.md) | Async TTL/LRU cache | B | §6, AC-05 (expiry, eviction) | `cache` |
| [T3](T3-scanner-orchestration.md) | `DependencyScanner.scan` / `assess` | C | §5, §6, §7, AC-03..08 | `orchestration` |
| [T4](T4-transport-integration-evidence.md) | Transport tests, shared HTTP limits, integration, evidence | D (integrator) | §3, §5 pool, §7 HTTP, §8, AC-01, 07, 08, 09, 10 | `transport` |

Replace the owner letters with names when assigning.

## File ownership

Each file below is written by exactly one ticket. No other ticket may edit it.

| Ticket | Production files | New test files | Docs |
| --- | --- | --- | --- |
| T1 | `src/dependency_intelligence/sources/pypi.py`, `sources/osv.py`, `sources/deps_dev.py` | `tests/test_sources_adapters.py` | none |
| T2 | `src/dependency_intelligence/cache.py` | `tests/test_cache_policy.py` | none |
| T3 | `src/dependency_intelligence/service.py` | `tests/test_scanner_orchestration.py` | none |
| T4 | `src/dependency_intelligence/bootstrap.py`, `tool_cli.py` | `tests/test_transport_boundaries.py` | `DECISIONS.md`, `EVIDENCE.md` |

### Frozen: nobody edits

- Protected by `AGENTS.md`: `SPEC.md`, `tests/test_contract.py`, `checks/readiness.py`.
- Stable interfaces that parallel work builds against: `ports.py`, `domain.py`,
  `errors.py`, `upstream_models.py`, `http.py`, `advisories.py`,
  `singleflight.py`, `reporting.py`, `manifest.py`.
- Shared files: `tests/fakes.py`, `pyproject.toml`, `uv.lock`, `sources/__init__.py`.
  If a test needs a helper, define it inside your own new test file.
- `api.py` and `agent_tool.py`: read-only. T4 may touch them only if a new test
  proves a spec gap, and only after the gap is recorded in `DECISIONS.md`.
- No new dependencies.

If a ticket needs a frozen file changed, stop and raise it with the integrator
(T4 owner). The change is a recorded interface decision, not a drive-by edit.

## Decisions

`DECISIONS.md` has a single owner (T4) to avoid merge conflicts. Each ticket file
lists "Decisions to hand to the integrator". The owner sends that text to the
integrator, who commits it to `DECISIONS.md` on the integration branch **before**
merging that ticket's code. Do not edit `DECISIONS.md` from a ticket branch.

## Merge order

One reviewed commit at a time. Run that ticket's focused check before the next
merge.

1. **T1** sources. No dependencies.
2. **T2** cache. No dependencies. T1 and T2 may swap places.
3. **T3** scanner. Must follow T2: one contract test uses the real `AsyncTTLCache`.
4. **T4** phase A (tests, HTTP limits) may merge at any point because it touches
   nobody else's files. T4 phase B (full gate, runtime evidence, `EVIDENCE.md`)
   runs last, after T1 to T3 are merged.

## Per-ticket workflow

1. Branch from the starter commit. Edit only the files your ticket owns.
2. Implement against the frozen interfaces; do not change any public signature.
3. Run your focused check and the static checks below.
4. Return the evidence listed in your ticket: diff summary, command output,
   unresolved risk, decisions to hand off.
5. Commit once with the imperative message given in the ticket.

Static checks every ticket runs before handing off:

```bash
uv run ruff check <owned files>
uv run ruff format --check <owned files>
uv run mypy src tests checks
```

## Integrator checks per merge

1. Diff touches only the ticket's owned files.
2. Ticket's focused check passes.
3. Contract mismatches are fixed at the interface, not with special cases.
4. No assertion in any existing test was weakened, skipped, or deleted.

Final gate after the last merge:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests checks
uv run pytest
```

## Specification coverage

| Spec | Ticket |
| --- | --- |
| §3.1 HTTP input, AC-01 | T4 (supplied `api.py`, new tests) |
| §3.2 agent-tool input, AC-01, AC-09 | T4 (supplied `agent_tool.py`/`manifest.py`, new tests) |
| §4.1 PyPI, AC-02 | T1 |
| §4.2 OSV, AC-02 | T1 |
| §4.3 deps.dev, AC-02, deps.dev overlap in AC-03 | T1 |
| §5 three-source start, package bound, order, cancellation, single-flight; AC-03, 04, 08 | T3 |
| §5 bounded HTTP pool | T4 |
| §6 cache structure; AC-05 expiry and eviction | T2 |
| §6 complete-only caching, `hit`/`miss`/`coalesced`; AC-05 single-flight | T3 |
| §7 source-failure mapping (`invalid_response`) | T1 |
| §7 `unexpected`, partial results, all-fail; AC-06, 07 | T3 |
| §7 HTTP 200/503/504 | T4 |
| §8 stable output | `reporting.py` (supplied), verified in T4 |
| AC-10 | each ticket lints its own files; T4 runs the full gate |

## Cross-ticket contract (stable, already in the repo)

- `ports.py`: `PackageMetadataSource`, `VulnerabilitySource`, `SupplyChainSource`,
  `AssessmentCache`. T1 produces the first three; T2 produces the fourth; T3
  consumes all four through the protocols only.
- `AsyncTTLCache[K, V]`: constructor plus `get`, `put`, `size` exactly as in
  `cache.py`. T3 depends on `get`/`put` only.
- `DependencyScanner`: constructor, `scan`, `assess` exactly as in `service.py`.
  `api.py` and `agent_tool.py` depend on `scan` only.
- Source adapters raise only `SourceRequestError` for upstream failures.
  T3 treats anything else as `unexpected`.

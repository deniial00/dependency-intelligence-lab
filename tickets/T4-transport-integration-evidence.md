# T4: Transport boundaries, composition, and evidence (integrator)

Owner: D (also the final integrator). Commits:
`Harden transport boundaries and share HTTP client limits`, then
`Record acceptance evidence`.
General rules: [README.md](README.md). Spec: `SPEC.md` §3, §5 (pool bound), §7
(HTTP status), §8; AC-01, AC-07, AC-08, AC-09, AC-10.

## Goal

1. Prove the supplied HTTP and agent-tool layers meet the spec, using tests that
   do not depend on T1 to T3.
2. Close the one gap found in supplied composition code.
3. Own integration order, `DECISIONS.md`, `EVIDENCE.md`, and the final quality gate.

## May modify

- `src/dependency_intelligence/bootstrap.py`
- `src/dependency_intelligence/tool_cli.py`
- new: `tests/test_transport_boundaries.py` (mark every test `@pytest.mark.transport`)
- `DECISIONS.md`, `EVIDENCE.md` (complete the templates; keep every required heading)

`api.py`, `agent_tool.py`, `manifest.py`, `reporting.py` are read-only unless a
new test proves a spec gap. Then make the smallest fix and record it in
`DECISIONS.md` first.

## Read-only context

`api.py`, `agent_tool.py`, `manifest.py`, `reporting.py`, `domain.py`,
`tests/test_contract.py` (transport tests), `fake_upstreams.py`, `EVIDENCE.md`,
`HANDOUT.md`.

## Phase A: independent of T1 to T3

Tests use a stub scanner object with an `async def scan(self, dependencies)`
method (as `NeverCalledScanner` does in the contract test): either return a
canned `ProjectScanReport`, raise `AssertionError` if called, or sleep forever.
No `service.py` is needed.

### Tests (AC-01, AC-07, AC-08, AC-09)

- **AC-01, HTTP:** `POST /v1/scans` returns 422 and the scanner is never called
  for each of: invalid name, invalid version, duplicate normalized names
  (`Demo_Pkg` and `demo-pkg`), non-PyPI ecosystem, empty list, 201 dependencies,
  unknown extra field. A valid body is accepted and names are normalized.
- **AC-07/08, HTTP:**
  - a `failed` report returns 503 with status `failed`;
  - complete and partial reports return 200;
  - a stub scanner that never finishes plus a tiny `scan_timeout_seconds` returns
    504 with `code == "scan_timeout"`, and the stub observes `CancelledError`.
- **AC-09, agent tool:** with a stub scanner that must not be called, rejection
  returns `invalid_manifest_path` for: absolute path, `..` component, hidden
  component, wrong filename, symlinked file escaping the workspace, symlinked
  directory escaping the workspace, missing file.
- **AC-09, shape and limits:** a valid manifest reaches the scanner and returns the
  same `report_to_dict` shape as the API. Unsupported lines return
  `invalid_manifest` with line numbers (URL, marker, extra, wildcard, duplicate
  normalized name, non-UTF-8, over 64,000 bytes, over 200 dependencies). The tool
  never writes into the workspace (compare the directory listing before and after).
- **§8 shape:** top-level keys and per-dependency keys of the report; `None` vs
  `[]` vulnerabilities projects to `null` vs `[]`.

### Gap fix: bounded HTTP pool on the agent-tool path (§5)

`tool_cli.py` builds `httpx.AsyncClient(timeout=...)` with no `httpx.Limits`, so
`MAX_HTTP_CONNECTIONS` is ignored and httpx's default of 100 applies. The API
lifespan in `bootstrap.py` does set limits. Extract one client factory in
`bootstrap.py` (settings in, `httpx.AsyncClient` out, with `Limits` and the
`User-Agent`) and use it from both the lifespan and `tool_cli.py`. Add a test
that the client built from `Settings(max_http_connections=N)` has pool limit `N`.

## Phase B: integration, after T1 to T3 are merged

1. Merge T1, T2, T3 in README order; inspect each diff against its owned files
   and run each focused check before the next merge.
2. Record each ticket's handed-off decisions in `DECISIONS.md` before merging that
   ticket. Fill the template: context, options, decision, consequences, ACs
   affected, verifying evidence.
3. Run the final gate (README) and record it in `EVIDENCE.md`.
4. Collect runtime evidence using the commands in `EVIDENCE.md` against
   `dependency-fake-upstreams` and `dependency-api`:
   - source and package calls overlap;
   - a second identical scan hits the cache and the `/_admin/calls` counters do
     not move;
   - `unavailable-osv-demo==1.0.0` gives HTTP 200, `partial`, `vulnerabilities:
     null`, and one retryable OSV failure;
   - `../requirements.lock` through the tool is rejected.
5. Fill the AC-01 to AC-10 trace table with commit hashes and test names.
6. Complete the human acceptance decision.

## Acceptance criteria

- Phase A: `uv run pytest -m transport tests/test_transport_boundaries.py` passes
  with no dependency on T1 to T3; lint and mypy pass.
- Phase B: the full gate passes; `EVIDENCE.md` contains concise real command
  output, no credentials, no whole transcripts.
- Every ticket was merged as its own commit, touching only its owned files.

## Decisions to record

- `ecosystem: "pypi"` (lowercase) is currently accepted because
  `DependencyCoordinate` casefolds it; the spec says "only the `PyPI` ecosystem".
  Record the interpretation, or tighten validation only if a test requires it.
- The shared client factory and where limits come from.

## Unresolved production risk to state in `EVIDENCE.md`

By default 8 packages x 4 requests can need 32 concurrent connections against a
24-connection pool. Excess requests queue on the pool, and httpx counts that wait
against `request_timeout_seconds`, so slow upstreams may surface queued requests
as `timeout`. This is from reading the code; reproduce before claiming it.

## Focused check

```bash
# phase A
uv run pytest -m transport tests/test_transport_boundaries.py
uv run ruff check src/dependency_intelligence/bootstrap.py src/dependency_intelligence/tool_cli.py tests/test_transport_boundaries.py
uv run ruff format --check src/dependency_intelligence/bootstrap.py src/dependency_intelligence/tool_cli.py tests/test_transport_boundaries.py
uv run mypy src tests checks
# phase B
uv run pytest -m transport
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests checks
uv run pytest
```

## Evidence to return

Phase A: diff summary, command output, decisions. Phase B: the filled
`EVIDENCE.md`, the trace table, and the acceptance decision.

## Do not

Edit `SPEC.md`, `tests/test_contract.py`, `checks/readiness.py`, `tests/fakes.py`,
`pyproject.toml`, or another ticket's production file during phase A. Do not
weaken or delete any existing test to get a green run.

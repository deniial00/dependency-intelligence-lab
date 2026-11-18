# T1: Source adapters

Owner: A. Commit: `Implement PyPI, OSV, and deps.dev adapters`.
General rules: [README.md](README.md). Spec: `SPEC.md` §4, §7; AC-02, AC-03.

## Goal

Make `PyPIClient`, `OSVClient`, and `DepsDevClient` translate upstream JSON into
the domain records, with malformed-but-valid JSON becoming an attributed
`invalid_response`.

## May modify

- `src/dependency_intelligence/sources/pypi.py`
- `src/dependency_intelligence/sources/osv.py`
- `src/dependency_intelligence/sources/deps_dev.py`
- new: `tests/test_sources_adapters.py` (mark every test `@pytest.mark.sources`)

## Read-only context

`ports.py` (protocols to satisfy), `http.py` (`JsonHttpClient.request_json`),
`upstream_models.py` (`PyPIResponse`, `OSVResponse`, `DepsMetadata`, `DepsGraph`,
`parse_response`), `advisories.py` (`merge_advisories`), `domain.py`,
`fake_upstreams.py` (URL and body shapes), `tests/test_contract.py`
(`test_source_adapters_translate_valid_responses`,
`test_deps_dev_requests_start_concurrently`).

## Requirements

### PyPI (§4.1)

- `GET {base}/pypi/{normalized-name}/json`.
- Return `PackageMetadata(latest_version, requires_python)`; `requires_python`
  may be `None`.
- An unparseable version is `invalid_response`, never a successful unknown value.
  The supplied model already validates this: use `parse_response`.

### OSV (§4.2)

- `POST {base}/v1/query` with body
  `{"package": {"name": <normalized>, "ecosystem": "PyPI"}, "version": <exact>}`.
- Missing `vulns` is a successful `()`.
- Convert entries to `Vulnerability` and pass them through `merge_advisories` so
  transitive alias chains collapse; return a tuple.

### deps.dev (§4.3)

- Call both endpoints:
  - `GET {base}/v3/systems/PYPI/packages/{name}/versions/{version}`
  - `GET {base}/v3/systems/PYPI/packages/{name}/versions/{version}:dependencies`
- The two requests must overlap: both are started before either is awaited to
  completion (AC-03).
- Return `SupplyChainInformation(licenses, direct_dependency_count, published_at)`.
  `direct_dependency_count` counts only nodes whose relation is `DIRECT`.
- Missing repeated fields are valid empty lists; a missing required structure,
  a graph with no or multiple `SELF` roots, or any graph or node error is
  `invalid_response` (the supplied `DepsGraph` validator enforces this).

## Acceptance criteria

- Valid fixture responses yield the specified records (contract test).
- Both deps.dev calls start before either finishes (contract test).
- New tests cover, per adapter: success, missing optional field, schema mismatch
  to `invalid_response` with correct `service`, and a propagated HTTP failure
  (404 to `not_found`, 503 to `unavailable`) keeping the right service.
- OSV with omitted `vulns` returns `()`, which is distinguishable from a failure.
- No upstream response body or credential appears in any failure detail.

## Watch-outs

- `GateHttp` in the contract test accepts exactly
  `request_json(service, method, url, *, json=None)`. Do not pass other kwargs.
- `asyncio.TaskGroup` raises `ExceptionGroup`. Unwrap so callers see one
  `SourceRequestError`; if both deps.dev calls fail, pick one deterministically.
  Cancel the sibling when one call fails.
- Use `parse_response` for every response; do not hand-validate dicts.
- Do not catch `CancelledError`.
- Build URLs from the normalized name and exact version; percent-encode path
  segments that can contain `+`.

## Focused check

```bash
uv run pytest -m sources
uv run ruff check src/dependency_intelligence/sources tests/test_sources_adapters.py
uv run ruff format --check src/dependency_intelligence/sources tests/test_sources_adapters.py
uv run mypy src tests checks
```

## Evidence to return

Diff summary, the command output above, any unresolved risk, and the decisions
below.

## Decisions to hand to the integrator

- Whether a deps.dev `versionKey` that differs from the requested coordinate is
  rejected as `invalid_response` (`VersionKey.coordinate()` is supplied but the
  spec is silent).
- Which failure surfaces when both deps.dev calls fail.
- OSV `next_page_token` pagination is not followed (the spec asks for one query).

## Do not

Edit `upstream_models.py`, `http.py`, `advisories.py`, `sources/__init__.py`,
`tests/fakes.py`, or any other ticket's file.

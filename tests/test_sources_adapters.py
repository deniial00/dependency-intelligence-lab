"""Behavior of the PyPI, OSV, and deps.dev adapters beyond the public contract."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest

from dependency_intelligence.domain import (
    DependencyCoordinate,
    FailureKind,
    ServiceName,
    SupplyChainInformation,
    Vulnerability,
)
from dependency_intelligence.errors import SourceRequestError
from dependency_intelligence.http import JsonHttpClient
from dependency_intelligence.sources import DepsDevClient, OSVClient, PyPIClient

from .fakes import StartGate

pytestmark = pytest.mark.sources

BASE = "https://fixture"
DEMO = DependencyCoordinate("demo", "1.0")
SECRET = "s3cret-token"

DEPS_METADATA_PATH = "/v3/systems/PYPI/packages/demo/versions/1.0"
DEPS_GRAPH_PATH = f"{DEPS_METADATA_PATH}:dependencies"


class FakeUpstream:
    """MockTransport handler that serves canned responses by decoded URL path."""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            return self.responses[request.url.path]
        except KeyError:
            raise AssertionError(f"unexpected request {request.method} {request.url}") from None


@asynccontextmanager
async def json_http(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[JsonHttpClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield JsonHttpClient(client, max_attempts=1)


def ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def version_key(name: str = "demo", version: str = "1.0") -> dict[str, str]:
    return {"system": "PYPI", "name": name, "version": version}


def dependency_graph(*relations: str) -> dict[str, Any]:
    nodes = [{"versionKey": version_key(), "relation": "SELF"}]
    nodes.extend(
        {"versionKey": version_key(f"dep-{index}"), "relation": relation}
        for index, relation in enumerate(relations)
    )
    return {"nodes": nodes}


async def fetch_pypi(http: JsonHttpClient, coordinate: DependencyCoordinate) -> object:
    return await PyPIClient(http, base_url=BASE).fetch_metadata(coordinate)


async def fetch_osv(http: JsonHttpClient, coordinate: DependencyCoordinate) -> object:
    return await OSVClient(http, base_url=BASE).fetch_vulnerabilities(coordinate)


async def fetch_deps_dev(http: JsonHttpClient, coordinate: DependencyCoordinate) -> object:
    return await DepsDevClient(http, base_url=BASE).fetch_supply_chain(coordinate)


Fetcher = Callable[[JsonHttpClient, DependencyCoordinate], Awaitable[object]]
FETCHERS = [
    pytest.param(ServiceName.PYPI, fetch_pypi, id="pypi"),
    pytest.param(ServiceName.OSV, fetch_osv, id="osv"),
    pytest.param(ServiceName.DEPS_DEV, fetch_deps_dev, id="deps.dev"),
]


async def failure_of(call: Awaitable[object]) -> SourceRequestError:
    with pytest.raises(SourceRequestError) as raised:
        await call
    return raised.value


# Behavior shared by every adapter


@pytest.mark.parametrize(("service", "fetch"), FETCHERS)
@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (404, FailureKind.NOT_FOUND, False),
        (429, FailureKind.RATE_LIMITED, True),
        (503, FailureKind.UNAVAILABLE, True),
    ],
)
async def test_http_failures_keep_service_attribution(
    service: ServiceName, fetch: Fetcher, status: int, kind: FailureKind, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"body containing {SECRET}")

    async with json_http(handler) as http:
        error = await failure_of(fetch(http, DEMO))

    assert error.failure.service is service
    assert error.failure.kind is kind
    assert error.failure.retryable is retryable
    assert error.failure.status_code == status
    assert SECRET not in error.failure.detail


@pytest.mark.parametrize(("service", "fetch"), FETCHERS)
async def test_schema_mismatch_is_invalid_response_without_leaking_the_body(
    service: ServiceName, fetch: Fetcher
) -> None:
    hostile = {
        "info": SECRET,
        "vulns": SECRET,
        "nodes": SECRET,
        "licenses": SECRET,
        "versionKey": SECRET,
    }

    async with json_http(lambda request: ok(hostile)) as http:
        error = await failure_of(fetch(http, DEMO))

    assert error.failure.service is service
    assert error.failure.kind is FailureKind.INVALID_RESPONSE
    assert error.failure.retryable is False
    assert SECRET not in error.failure.detail
    assert SECRET not in str(error)


# PyPI


async def test_pypi_requests_the_normalized_name_and_returns_metadata() -> None:
    upstream = FakeUpstream(
        {
            "/pypi/demo-pkg/json": ok(
                {"info": {"name": "Demo_Pkg", "version": "2.1.0", "requires_python": ">=3.9"}}
            )
        }
    )

    async with json_http(upstream) as http:
        metadata = await PyPIClient(http, base_url=BASE).fetch_metadata(
            DependencyCoordinate("Demo_Pkg", "1.0")
        )

    assert (metadata.latest_version, metadata.requires_python) == ("2.1.0", ">=3.9")
    assert [(request.method, request.url.path) for request in upstream.requests] == [
        ("GET", "/pypi/demo-pkg/json")
    ]


@pytest.mark.parametrize("info", [{"version": "1.2"}, {"version": "1.2", "requires_python": None}])
async def test_pypi_requires_python_may_be_absent(info: dict[str, Any]) -> None:
    upstream = FakeUpstream({"/pypi/demo/json": ok({"info": info})})

    async with json_http(upstream) as http:
        metadata = await PyPIClient(http, base_url=BASE).fetch_metadata(DEMO)

    assert metadata.latest_version == "1.2"
    assert metadata.requires_python is None


@pytest.mark.parametrize(
    "payload",
    [
        {"info": {"version": "not-a-version"}},
        {"info": {"version": ""}},
        {"info": {"version": 2}},
        {"info": {"version": "1.0", "requires_python": 3}},
        {"info": {}},
        {"info": None},
        {},
        [],
    ],
)
async def test_pypi_rejects_malformed_responses(payload: Any) -> None:
    upstream = FakeUpstream({"/pypi/demo/json": ok(payload)})

    async with json_http(upstream) as http:
        error = await failure_of(PyPIClient(http, base_url=BASE).fetch_metadata(DEMO))

    assert error.failure.service is ServiceName.PYPI
    assert error.failure.kind is FailureKind.INVALID_RESPONSE


# OSV


async def test_osv_posts_the_exact_coordinate() -> None:
    upstream = FakeUpstream({"/v1/query": ok({})})

    async with json_http(upstream) as http:
        await OSVClient(http, base_url=BASE).fetch_vulnerabilities(
            DependencyCoordinate("Demo_Pkg", "1.0.post1")
        )

    (request,) = upstream.requests
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "package": {"name": "demo-pkg", "ecosystem": "PyPI"},
        "version": "1.0.post1",
    }


@pytest.mark.parametrize("payload", [{}, {"vulns": []}, {"other": "ignored"}])
async def test_osv_empty_answer_is_a_successful_empty_tuple(payload: Any) -> None:
    upstream = FakeUpstream({"/v1/query": ok(payload)})

    async with json_http(upstream) as http:
        advisories = await OSVClient(http, base_url=BASE).fetch_vulnerabilities(DEMO)

    assert advisories == ()
    assert advisories is not None


@pytest.mark.parametrize("reverse", [False, True])
async def test_osv_merges_transitive_aliases_independent_of_input_order(reverse: bool) -> None:
    records = [
        {"id": "PYSEC-9", "aliases": ["CVE-9"]},
        {"id": "CVE-9", "aliases": ["GHSA-9"], "summary": "kept summary"},
        {"id": "OSV-2", "aliases": []},
    ]
    upstream = FakeUpstream({"/v1/query": ok({"vulns": records[::-1] if reverse else records})})

    async with json_http(upstream) as http:
        advisories = await OSVClient(http, base_url=BASE).fetch_vulnerabilities(DEMO)

    assert advisories == (
        Vulnerability("OSV-2", (), None),
        Vulnerability("CVE-9", ("GHSA-9", "PYSEC-9"), "kept summary"),
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"vulns": "invalid"},
        {"vulns": None},
        {"vulns": [{"aliases": ["CVE-1"]}]},
        {"vulns": [{"id": ""}]},
        {"vulns": [{"id": "GHSA-1", "aliases": [" "]}]},
        {"vulns": [{"id": "GHSA-1", "aliases": "CVE-1"}]},
        {"vulns": ["GHSA-1"]},
        [],
    ],
)
async def test_osv_rejects_malformed_responses(payload: Any) -> None:
    upstream = FakeUpstream({"/v1/query": ok(payload)})

    async with json_http(upstream) as http:
        error = await failure_of(OSVClient(http, base_url=BASE).fetch_vulnerabilities(DEMO))

    assert error.failure.service is ServiceName.OSV
    assert error.failure.kind is FailureKind.INVALID_RESPONSE


# deps.dev


async def test_deps_dev_counts_only_direct_dependencies() -> None:
    upstream = FakeUpstream(
        {
            DEPS_METADATA_PATH: ok(
                {
                    "versionKey": version_key(),
                    "licenses": ["MIT", "Apache-2.0"],
                    "publishedAt": "2024-01-01T00:00:00Z",
                }
            ),
            DEPS_GRAPH_PATH: ok(dependency_graph("DIRECT", "INDIRECT", "DIRECT", "INDIRECT")),
        }
    )

    async with json_http(upstream) as http:
        result = await DepsDevClient(http, base_url=BASE).fetch_supply_chain(DEMO)

    assert result == SupplyChainInformation(("MIT", "Apache-2.0"), 2, "2024-01-01T00:00:00Z")
    assert {request.url.path for request in upstream.requests} == {
        DEPS_METADATA_PATH,
        DEPS_GRAPH_PATH,
    }


async def test_deps_dev_missing_repeated_fields_are_valid_empty_values() -> None:
    upstream = FakeUpstream(
        {
            DEPS_METADATA_PATH: ok({"versionKey": version_key()}),
            DEPS_GRAPH_PATH: ok(dependency_graph()),
        }
    )

    async with json_http(upstream) as http:
        result = await DepsDevClient(http, base_url=BASE).fetch_supply_chain(DEMO)

    assert result == SupplyChainInformation((), 0, None)


async def test_deps_dev_percent_encodes_the_version_segment() -> None:
    coordinate = DependencyCoordinate("demo", "1.0+local.1")
    upstream = FakeUpstream(
        {
            "/v3/systems/PYPI/packages/demo/versions/1.0+local.1": ok(
                {"versionKey": version_key(version="1.0+local.1")}
            ),
            "/v3/systems/PYPI/packages/demo/versions/1.0+local.1:dependencies": ok(
                dependency_graph()
            ),
        }
    )

    async with json_http(upstream) as http:
        await DepsDevClient(http, base_url=BASE).fetch_supply_chain(coordinate)

    assert {request.url.raw_path for request in upstream.requests} == {
        b"/v3/systems/PYPI/packages/demo/versions/1.0%2Blocal.1",
        b"/v3/systems/PYPI/packages/demo/versions/1.0%2Blocal.1:dependencies",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"versionKey": version_key(), "licenses": "MIT"},
        {"versionKey": version_key(), "publishedAt": 5},
        {"versionKey": {"system": "NPM", "name": "demo", "version": "1.0"}},
        [],
    ],
)
async def test_deps_dev_rejects_malformed_metadata(metadata: Any) -> None:
    upstream = FakeUpstream(
        {DEPS_METADATA_PATH: ok(metadata), DEPS_GRAPH_PATH: ok(dependency_graph("DIRECT"))}
    )

    async with json_http(upstream) as http:
        error = await failure_of(DepsDevClient(http, base_url=BASE).fetch_supply_chain(DEMO))

    assert error.failure.service is ServiceName.DEPS_DEV
    assert error.failure.kind is FailureKind.INVALID_RESPONSE


def _graph_with_node_errors() -> dict[str, Any]:
    graph = dependency_graph("DIRECT")
    graph["nodes"][1]["errors"] = ["resolution failed"]
    return graph


@pytest.mark.parametrize(
    "graph",
    [
        {},
        {"nodes": []},
        {"nodes": [{"versionKey": version_key("dep"), "relation": "DIRECT"}]},
        dependency_graph("SELF"),
        {**dependency_graph("DIRECT"), "error": "graph incomplete"},
        _graph_with_node_errors(),
        dependency_graph("SIDEWAYS"),
        [],
    ],
    ids=[
        "missing-nodes",
        "empty-nodes",
        "no-root",
        "two-roots",
        "graph-error",
        "node-error",
        "unknown-relation",
        "not-an-object",
    ],
)
async def test_deps_dev_rejects_malformed_or_incomplete_graphs(graph: Any) -> None:
    upstream = FakeUpstream(
        {DEPS_METADATA_PATH: ok({"versionKey": version_key()}), DEPS_GRAPH_PATH: ok(graph)}
    )

    async with json_http(upstream) as http:
        error = await failure_of(DepsDevClient(http, base_url=BASE).fetch_supply_chain(DEMO))

    assert error.failure.service is ServiceName.DEPS_DEV
    assert error.failure.kind is FailureKind.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("failing_path", "status", "kind"),
    [
        (DEPS_METADATA_PATH, 404, FailureKind.NOT_FOUND),
        (DEPS_GRAPH_PATH, 503, FailureKind.UNAVAILABLE),
    ],
)
async def test_deps_dev_fails_when_either_endpoint_fails(
    failing_path: str, status: int, kind: FailureKind
) -> None:
    responses = {
        DEPS_METADATA_PATH: ok({"versionKey": version_key()}),
        DEPS_GRAPH_PATH: ok(dependency_graph()),
    }
    responses[failing_path] = httpx.Response(status)

    async with json_http(FakeUpstream(responses)) as http:
        error = await failure_of(DepsDevClient(http, base_url=BASE).fetch_supply_chain(DEMO))

    assert error.failure.service is ServiceName.DEPS_DEV
    assert error.failure.kind is kind
    assert error.failure.status_code == status


class ScriptedHttp:
    """JsonHttpClient stand-in whose metadata and graph behavior are scripted."""

    def __init__(
        self,
        metadata: Callable[[], Awaitable[Any]],
        graph: Callable[[], Awaitable[Any]],
    ) -> None:
        self._metadata = metadata
        self._graph = graph

    async def request_json(
        self,
        service: ServiceName,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> Any:
        return await (self._graph if url.endswith(":dependencies") else self._metadata)()

    def client(self) -> DepsDevClient:
        return DepsDevClient(cast(JsonHttpClient, self), base_url=BASE)


def failing(kind: FailureKind, *, after_turns: int = 0) -> Callable[[], Awaitable[Any]]:
    async def call() -> Any:
        for _ in range(after_turns):
            await asyncio.sleep(0)
        raise SourceRequestError(ServiceName.DEPS_DEV, kind, f"simulated {kind}", retryable=False)

    return call


async def test_deps_dev_failures_in_the_same_pass_are_ordered_by_endpoint() -> None:
    # Both requests fail before the group reacts: the graph a turn earlier than metadata.
    http = ScriptedHttp(
        metadata=failing(FailureKind.UNAVAILABLE, after_turns=1),
        graph=failing(FailureKind.NOT_FOUND),
    )

    error = await failure_of(http.client().fetch_supply_chain(DEMO))

    assert error.failure.kind is FailureKind.UNAVAILABLE


async def test_deps_dev_reports_the_first_failure_and_cancels_a_later_one() -> None:
    # Fail-fast: a graph failure cancels metadata before it can fail with its own error.
    cancelled = False

    async def metadata_failing_late() -> Any:
        nonlocal cancelled
        try:
            for _ in range(10):
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            cancelled = True
            raise
        raise SourceRequestError(
            ServiceName.DEPS_DEV, FailureKind.UNAVAILABLE, "too late", retryable=True
        )

    http = ScriptedHttp(metadata=metadata_failing_late, graph=failing(FailureKind.NOT_FOUND))

    error = await failure_of(http.client().fetch_supply_chain(DEMO))

    assert error.failure.kind is FailureKind.NOT_FOUND
    assert cancelled


async def test_deps_dev_failure_cancels_the_other_request_and_is_not_grouped() -> None:
    cancelled = asyncio.Event()

    async def hang() -> Any:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    http = ScriptedHttp(metadata=hang, graph=failing(FailureKind.TIMEOUT))

    error = await failure_of(http.client().fetch_supply_chain(DEMO))

    assert error.failure.kind is FailureKind.TIMEOUT
    assert cancelled.is_set()


async def test_deps_dev_unexpected_errors_are_not_wrapped_in_an_exception_group() -> None:
    async def explode() -> Any:
        raise RuntimeError("boom")

    async def never() -> Any:
        await asyncio.Event().wait()

    http = ScriptedHttp(metadata=never, graph=explode)

    with pytest.raises(RuntimeError, match="boom"):
        await http.client().fetch_supply_chain(DEMO)


async def test_cancelling_deps_dev_cancels_both_requests() -> None:
    gate = StartGate(2)

    async def wait() -> Any:
        await gate.wait()

    http = ScriptedHttp(metadata=wait, graph=wait)
    task = asyncio.create_task(http.client().fetch_supply_chain(DEMO))

    await asyncio.wait_for(gate.all_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.cancelled == 2

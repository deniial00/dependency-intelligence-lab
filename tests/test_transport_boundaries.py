"""HTTP and agent-tool boundaries: input validation, status mapping, deadlines, pool bound.

The validation and status tests use stub scanners, so they exercise only the transports.
The pool tests run the real composition root against a local threaded HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from dependency_intelligence.agent_tool import DependencyScanTool
from dependency_intelligence.api import create_app
from dependency_intelligence.bootstrap import build_app
from dependency_intelligence.domain import (
    CacheStatus,
    DependencyCoordinate,
    DependencyScanResult,
    FailureKind,
    PackageAssessment,
    PackageMetadata,
    ProjectScanReport,
    ServiceName,
    SourceFailure,
    SupplyChainInformation,
    Vulnerability,
)
from dependency_intelligence.reporting import report_to_dict
from dependency_intelligence.service import DependencyScanner
from dependency_intelligence.tool_cli import _run

from .fakes import (
    FakeMetadataSource,
    FakeSupplyChainSource,
    FakeVulnerabilitySource,
    MemoryCache,
    StartGate,
)

pytestmark = pytest.mark.transport

GOOD = {"name": "demo", "version": "1.0"}
METADATA = PackageMetadata("2.0", ">=3.9")
SUPPLY_CHAIN = SupplyChainInformation(("MIT",), 2, "2024-01-01T00:00:00Z")


# Stub scanners and report builders


class CannedScanner:
    def __init__(self, report: ProjectScanReport) -> None:
        self.report = report
        self.calls: list[tuple[DependencyCoordinate, ...]] = []

    async def scan(self, dependencies: object) -> ProjectScanReport:
        self.calls.append(tuple(cast(list[DependencyCoordinate], dependencies)))
        return self.report


class ForeverScanner:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def scan(self, dependencies: object) -> ProjectScanReport:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


class NeverCalledScanner:
    async def scan(self, dependencies: object) -> ProjectScanReport:
        raise AssertionError("the scanner must not be reached")


def as_scanner(stub: object) -> DependencyScanner:
    return cast(DependencyScanner, stub)


def complete_result(
    name: str = "demo", *, vulnerabilities: tuple[Vulnerability, ...] = ()
) -> DependencyScanResult:
    coordinate = DependencyCoordinate(name, "1.0")
    assessment = PackageAssessment(coordinate, METADATA, vulnerabilities, SUPPLY_CHAIN)
    return DependencyScanResult(coordinate, assessment, (), CacheStatus.MISS)


OSV_UNAVAILABLE = SourceFailure(
    ServiceName.OSV, FailureKind.UNAVAILABLE, True, "service returned HTTP 503", 503
)


def partial_result(name: str = "demo") -> DependencyScanResult:
    coordinate = DependencyCoordinate(name, "1.0")
    assessment = PackageAssessment(coordinate, METADATA, None, SUPPLY_CHAIN)
    return DependencyScanResult(coordinate, assessment, (OSV_UNAVAILABLE,), CacheStatus.MISS)


def failed_result(name: str = "demo") -> DependencyScanResult:
    failures = tuple(
        SourceFailure(service, FailureKind.TIMEOUT, True, "request timed out")
        for service in (ServiceName.PYPI, ServiceName.OSV, ServiceName.DEPS_DEV)
    )
    return DependencyScanResult(DependencyCoordinate(name, "1.0"), None, failures, CacheStatus.MISS)


def report_of(*results: DependencyScanResult) -> ProjectScanReport:
    return ProjectScanReport(results)


def deps(*items: dict[str, Any]) -> dict[str, Any]:
    return {"dependencies": list(items)}


@asynccontextmanager
async def api(
    scanner: object, *, scan_timeout_seconds: float = 30.0
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(scanner=as_scanner(scanner), scan_timeout_seconds=scan_timeout_seconds)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# AC-01: HTTP input validation

INVALID_BODIES: dict[str, dict[str, Any]] = {
    "invalid-name": deps({"name": "bad name!", "version": "1.0"}),
    "name-starts-with-dash": deps({"name": "-demo", "version": "1.0"}),
    "blank-name": deps({"name": "   ", "version": "1.0"}),
    "invalid-version": deps({"name": "demo", "version": "not-a-version"}),
    "wildcard-version": deps({"name": "demo", "version": "1.*"}),
    "specifier-as-version": deps({"name": "demo", "version": ">=1.0"}),
    "blank-version": deps({"name": "demo", "version": " "}),
    "one-bad-among-good": deps(GOOD, {"name": "other", "version": "nope"}),
    "duplicate-normalized-names": deps(
        {"name": "Demo_Pkg", "version": "1.0"}, {"name": "demo-pkg", "version": "2.0"}
    ),
    "non-pypi-ecosystem": deps({**GOOD, "ecosystem": "npm"}),
    "empty-list": deps(),
    "too-many-dependencies": deps(
        *({"name": f"pkg-{index}", "version": "1.0"} for index in range(201))
    ),
    "extra-dependency-field": deps({**GOOD, "extras": ["security"]}),
    "extra-top-level-field": {**deps(GOOD), "options": {}},
    "missing-dependencies": {},
    "dependencies-not-a-list": {"dependencies": "demo==1.0"},
}


@pytest.mark.parametrize("body", list(INVALID_BODIES.values()), ids=list(INVALID_BODIES))
async def test_invalid_request_is_rejected_before_any_scan(body: dict[str, Any]) -> None:
    scanner = CannedScanner(report_of(complete_result()))

    async with api(scanner) as client:
        response = await client.post("/v1/scans", json=body)

    assert response.status_code == 422
    assert scanner.calls == []


async def test_malformed_json_is_rejected_before_any_scan() -> None:
    scanner = CannedScanner(report_of(complete_result()))

    async with api(scanner) as client:
        response = await client.post(
            "/v1/scans", content=b'{"dependencies": [', headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 422
    assert scanner.calls == []


async def test_valid_request_reaches_the_scanner_with_normalized_coordinates() -> None:
    scanner = CannedScanner(report_of(complete_result("demo-pkg"), complete_result("other")))
    body = deps({"name": " Demo_Pkg ", "version": " 1.0 "}, {"name": "other", "version": "2.0"})

    async with api(scanner) as client:
        response = await client.post("/v1/scans", json=body)

    assert response.status_code == 200
    assert [(item.name, item.version, item.ecosystem) for item in scanner.calls[0]] == [
        ("demo-pkg", "1.0", "PyPI"),
        ("other", "2.0", "PyPI"),
    ]


async def test_lowercase_ecosystem_is_accepted_and_normalized() -> None:
    scanner = CannedScanner(report_of(complete_result()))

    async with api(scanner) as client:
        response = await client.post("/v1/scans", json=deps({**GOOD, "ecosystem": "pypi"}))

    assert response.status_code == 200
    assert [item.ecosystem for item in scanner.calls[0]] == ["PyPI"]


async def test_two_hundred_dependencies_are_accepted() -> None:
    scanner = CannedScanner(report_of(complete_result()))
    body = deps(*({"name": f"pkg-{index}", "version": "1.0"} for index in range(200)))

    async with api(scanner) as client:
        response = await client.post("/v1/scans", json=body)

    assert response.status_code == 200
    assert len(scanner.calls[0]) == 200


# AC-07 and AC-08: HTTP status mapping and deadline


@pytest.mark.parametrize(
    ("report", "status_code", "status"),
    [
        (report_of(complete_result()), 200, "complete"),
        (report_of(partial_result()), 200, "partial"),
        (report_of(failed_result(), partial_result("other")), 200, "partial"),
        (report_of(failed_result()), 503, "failed"),
    ],
    ids=["complete", "partial", "failed-package-among-useful-ones", "failed"],
)
async def test_report_status_maps_to_http_status(
    report: ProjectScanReport, status_code: int, status: str
) -> None:
    async with api(CannedScanner(report)) as client:
        response = await client.post("/v1/scans", json=deps(GOOD))

    assert response.status_code == status_code
    assert response.json()["status"] == status


async def test_failed_report_names_every_failed_source_and_keeps_unknowns_null() -> None:
    async with api(CannedScanner(report_of(failed_result()))) as client:
        response = await client.post("/v1/scans", json=deps(GOOD))

    body = response.json()
    assert {failure["service"] for failure in body["failures"]} == {"pypi", "osv", "deps.dev"}
    assert all(failure["kind"] == "timeout" for failure in body["failures"])
    (dependency,) = body["dependencies"]
    assert dependency["update_status"] == "unknown"
    assert dependency["complete"] is False
    for field in ("latest_version", "licenses", "published_at", "vulnerabilities"):
        assert dependency[field] is None


async def test_deadline_returns_504_and_cancels_the_scanner() -> None:
    scanner = ForeverScanner()

    async with api(scanner, scan_timeout_seconds=0.05) as client:
        response = await client.post("/v1/scans", json=deps(GOOD))

    assert response.status_code == 504
    assert response.json()["code"] == "scan_timeout"
    assert response.json()["status"] == "failed"
    assert scanner.cancelled.is_set()


async def test_deadline_cancels_outstanding_source_calls_of_the_real_scanner() -> None:
    gate = StartGate(3)
    scanner = DependencyScanner(
        metadata_source=FakeMetadataSource(gate=gate),
        vulnerability_source=FakeVulnerabilitySource(gate=gate),
        supply_chain_source=FakeSupplyChainSource(gate=gate),
        cache=MemoryCache(),
    )

    async with api(scanner, scan_timeout_seconds=0.05) as client:
        response = await client.post("/v1/scans", json=deps(GOOD))
    for _ in range(10):
        if gate.cancelled == 3:
            break
        await asyncio.sleep(0)

    assert response.status_code == 504
    assert gate.starts == 3
    assert gate.cancelled == 3


# §8: stable output


async def test_report_projection_keeps_null_and_empty_vulnerabilities_apart() -> None:
    advisory = Vulnerability("GHSA-1", ("CVE-1",), "summary")
    report = report_of(
        complete_result("found", vulnerabilities=(advisory,)),
        complete_result("clean"),
        partial_result("unknown"),
    )

    async with api(CannedScanner(report)) as client:
        body = (await client.post("/v1/scans", json=deps(GOOD))).json()

    assert set(body) == {"status", "summary", "dependencies", "failures"}
    assert body["status"] == "partial"
    assert body["summary"] == {
        "total_dependencies": 3,
        "vulnerable_dependencies": 1,
        "outdated_dependencies": 3,
        "failure_count": 1,
    }
    assert [dependency["name"] for dependency in body["dependencies"]] == [
        "found",
        "clean",
        "unknown",
    ]
    for dependency in body["dependencies"]:
        assert set(dependency) == {
            "name",
            "installed_version",
            "ecosystem",
            "latest_version",
            "requires_python",
            "update_status",
            "licenses",
            "direct_dependency_count",
            "published_at",
            "vulnerabilities",
            "cache_status",
            "complete",
            "failures",
        }
    found, clean, unknown = body["dependencies"]
    assert found["vulnerabilities"] == [
        {"id": "GHSA-1", "aliases": ["CVE-1"], "summary": "summary"}
    ]
    assert clean["vulnerabilities"] == []
    assert unknown["vulnerabilities"] is None
    assert unknown["complete"] is False
    assert unknown["licenses"] == ["MIT"]
    assert unknown["failures"] == body["failures"]
    assert body["failures"] == [
        {
            "dependency": "unknown==1.0",
            "service": "osv",
            "kind": "unavailable",
            "retryable": True,
            "detail": "service returned HTTP 503",
            "status_code": 503,
        }
    ]


# AC-09: agent-tool workspace boundary


@dataclass(frozen=True)
class Layout:
    workspace: Path
    outside: Path


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    for directory in (
        workspace / "sub" / ".cache",
        workspace / ".hidden",
        workspace / "link-file",
        workspace / "dir-lock" / "requirements.lock",
        outside,
    ):
        directory.mkdir(parents=True)
    (outside / "requirements.lock").write_text("secret==1.0\n", encoding="utf-8")
    for name in (".hidden/requirements.lock", "sub/.cache/requirements.lock", "requirements.txt"):
        (workspace / name).write_text("demo==1.0\n", encoding="utf-8")
    (workspace / "link-file" / "requirements.lock").symlink_to(outside / "requirements.lock")
    (workspace / "link-dir").symlink_to(outside, target_is_directory=True)
    return Layout(workspace, outside)


def tool_for(workspace: Path, scanner: object, **options: float) -> DependencyScanTool:
    return DependencyScanTool(workspace_root=workspace, scanner=as_scanner(scanner), **options)


REJECTED_PATHS = {
    "parent": "../outside/requirements.lock",
    "parent-in-the-middle": "sub/../requirements.lock",
    "hidden-directory": ".hidden/requirements.lock",
    "hidden-nested-directory": "sub/.cache/requirements.lock",
    "other-filename": "requirements.txt",
    "other-filename-suffix": "sub/requirements.lock.bak",
    "symlinked-file-escaping": "link-file/requirements.lock",
    "symlinked-directory-escaping": "link-dir/requirements.lock",
    "directory-instead-of-file": "dir-lock/requirements.lock",
    "missing-file": "missing/requirements.lock",
}


@pytest.mark.parametrize("path", list(REJECTED_PATHS.values()), ids=list(REJECTED_PATHS))
async def test_tool_rejects_paths_outside_the_boundary_without_scanning(
    layout: Layout, path: str
) -> None:
    result = await tool_for(layout.workspace, NeverCalledScanner()).execute(path)

    assert result["status"] == "invalid_manifest_path"
    assert "secret" not in json.dumps(result)


async def test_tool_rejects_an_absolute_path_without_scanning(layout: Layout) -> None:
    absolute = str(layout.outside / "requirements.lock")

    result = await tool_for(layout.workspace, NeverCalledScanner()).execute(absolute)

    assert result["status"] == "invalid_manifest_path"


async def test_tool_returns_the_api_report_shape_for_a_valid_manifest(layout: Layout) -> None:
    (layout.workspace / "requirements.lock").write_text(
        "# pinned\n\nDemo_Pkg==1.0  # comment\nother==2.0\n", encoding="utf-8"
    )
    report = report_of(complete_result("demo-pkg"), partial_result("other"))
    scanner = CannedScanner(report)

    tool_result = await tool_for(layout.workspace, scanner).execute()
    async with api(scanner) as client:
        api_body = (
            await client.post("/v1/scans", json=deps({"name": "demo-pkg", "version": "1.0"}, GOOD))
        ).json()

    assert [(item.name, item.version) for item in scanner.calls[0]] == [
        ("demo-pkg", "1.0"),
        ("other", "2.0"),
    ]
    assert tool_result == report_to_dict(report)
    assert tool_result == api_body


async def test_tool_accepts_nested_and_in_workspace_symlinked_manifests(layout: Layout) -> None:
    (layout.workspace / "sub" / "requirements.lock").write_text("demo==1.0\n", encoding="utf-8")
    (layout.workspace / "alias").mkdir()
    (layout.workspace / "alias" / "requirements.lock").symlink_to(
        layout.workspace / "sub" / "requirements.lock"
    )
    scanner = CannedScanner(report_of(complete_result()))
    tool = tool_for(layout.workspace, scanner)

    nested = await tool.execute("sub/requirements.lock")
    aliased = await tool.execute("alias/requirements.lock")

    assert nested["status"] == aliased["status"] == "complete"
    assert len(scanner.calls) == 2


MANIFEST_ISSUES = {
    "url": ("demo @ https://example.com/demo-1.0.whl\n", [(1, "URL")]),
    "marker": ('demo==1.0; python_version < "3.12"\n', [(1, "markers")]),
    "extra": ("demo[security]==1.0\n", [(1, "extras")]),
    "wildcard": ("demo==1.*\n", [(1, "wildcard")]),
    "range": ("demo>=1.0\n", [(1, "exact ==")]),
    "two-specifiers": ("demo==1.0,!=1.1\n", [(1, "exact ==")]),
    "invalid-version": ("demo==banana\n", [(1, "invalid requirement syntax")]),
    "invalid-syntax": ("this is not a requirement\n", [(1, "invalid requirement syntax")]),
    "line-numbers-skip-comments": ("# header\n\ngood==1.0\ndemo>=1\n", [(4, "exact ==")]),
    "duplicate-normalized-name": ("Demo_Pkg==1.0\ndemo-pkg==2.0\n", [(2, "duplicate")]),
    "every-bad-line-is-reported": ("a>=1\nb[x]==1\n", [(1, "exact =="), (2, "extras")]),
    "no-dependencies": ("# nothing pinned here\n", [(0, "no dependencies")]),
}


@pytest.mark.parametrize(
    ("content", "expected"), list(MANIFEST_ISSUES.values()), ids=list(MANIFEST_ISSUES)
)
async def test_tool_reports_unsupported_manifest_lines_with_line_numbers(
    layout: Layout, content: str, expected: list[tuple[int, str]]
) -> None:
    (layout.workspace / "requirements.lock").write_text(content, encoding="utf-8")

    result = await tool_for(layout.workspace, NeverCalledScanner()).execute()

    assert result["status"] == "invalid_manifest"
    assert [issue["line_number"] for issue in result["issues"]] == [line for line, _ in expected]
    for issue, (_, fragment) in zip(result["issues"], expected, strict=True):
        assert fragment in issue["reason"]


def pinned(count: int) -> str:
    return "".join(f"pkg-{index}==1.0\n" for index in range(count))


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"demo==1.0\n\xff\xfe\n", "UTF-8"),
        (b"#" * 64_001, "exceeds 64000 bytes"),
        (pinned(201).encode(), "more than 200"),
    ],
    ids=["not-utf-8", "over-64000-bytes", "over-200-dependencies"],
)
async def test_tool_rejects_manifests_over_the_limits(
    layout: Layout, raw: bytes, fragment: str
) -> None:
    (layout.workspace / "requirements.lock").write_bytes(raw)

    result = await tool_for(layout.workspace, NeverCalledScanner()).execute()

    assert result["status"] == "invalid_manifest"
    assert [issue["line_number"] for issue in result["issues"]] == [0]
    assert fragment in result["issues"][0]["reason"]


@pytest.mark.parametrize("count", [1, 200])
async def test_tool_accepts_manifests_at_the_limits(layout: Layout, count: int) -> None:
    padding = b"#" * (64_000 - len(pinned(count))) if count == 1 else b""
    (layout.workspace / "requirements.lock").write_bytes(pinned(count).encode() + padding)
    scanner = CannedScanner(report_of(complete_result()))

    result = await tool_for(layout.workspace, scanner).execute()

    assert result["status"] == "complete"
    assert len(scanner.calls[0]) == count


async def test_tool_never_writes_to_the_workspace(layout: Layout) -> None:
    (layout.workspace / "requirements.lock").write_text("demo==1.0\n", encoding="utf-8")

    def snapshot() -> dict[str, tuple[bool, bytes | None, int]]:
        return {
            str(path.relative_to(layout.workspace)): (
                path.is_dir(),
                None if path.is_dir() else path.read_bytes(),
                path.lstat().st_mtime_ns,
            )
            for path in sorted(layout.workspace.rglob("*"))
        }

    before = snapshot()
    tool = tool_for(layout.workspace, CannedScanner(report_of(complete_result())))
    await tool.execute()
    await tool.execute("../outside/requirements.lock")

    assert snapshot() == before


async def test_tool_deadline_returns_scan_timeout_and_cancels_the_scanner(layout: Layout) -> None:
    (layout.workspace / "requirements.lock").write_text("demo==1.0\n", encoding="utf-8")
    scanner = ForeverScanner()

    result = await tool_for(layout.workspace, scanner, scan_timeout_seconds=0.05).execute()

    assert result["status"] == "failed"
    assert result["code"] == "scan_timeout"
    assert scanner.cancelled.is_set()


# SPEC 5: the shared HTTP pool stays bounded on every composition path


class _Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.open_connections = 0
        self.peak_connections = 0
        self.requests = 0

    @contextmanager
    def connection(self) -> Iterator[None]:
        with self._lock:
            self.open_connections += 1
            self.peak_connections = max(self.peak_connections, self.open_connections)
        try:
            yield
        finally:
            with self._lock:
                self.open_connections -= 1

    def request(self) -> None:
        with self._lock:
            self.requests += 1


class _Server(ThreadingHTTPServer):
    """One local server standing in for PyPI, OSV, and deps.dev at once."""

    daemon_threads = True

    def __init__(self, delay_seconds: float) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.delay_seconds = delay_seconds
        self.stats = _Stats()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    key = {"system": "PYPI", "name": "demo", "version": "1.0"}

    @property
    def upstream(self) -> _Server:
        return cast(_Server, self.server)

    def handle(self) -> None:
        with self.upstream.stats.connection():
            super().handle()

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._answer()

    def _answer(self) -> None:
        self.upstream.stats.request()
        time.sleep(self.upstream.delay_seconds)
        if self.path.startswith("/pypi/"):
            payload: dict[str, Any] = {"info": {"version": "2.0", "requires_python": ">=3.9"}}
        elif self.path == "/v1/query":
            payload = {}
        elif self.path.endswith(":dependencies"):
            payload = {"nodes": [{"versionKey": self.key, "relation": "SELF"}]}
        else:
            payload = {"versionKey": self.key, "licenses": ["MIT"]}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def upstream() -> Iterator[_Server]:
    server = _Server(delay_seconds=0.02)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def point_environment_at(
    monkeypatch: pytest.MonkeyPatch, server: _Server, *, max_connections: int
) -> None:
    for name in ("PYPI_BASE_URL", "OSV_BASE_URL", "DEPS_DEV_BASE_URL"):
        monkeypatch.setenv(name, server.base_url)
    monkeypatch.setenv("MAX_HTTP_CONNECTIONS", str(max_connections))
    monkeypatch.setenv("MAX_CONCURRENT_PACKAGES", "6")
    for name in (
        "REQUEST_TIMEOUT_SECONDS",
        "SCAN_TIMEOUT_SECONDS",
        "CACHE_TTL_SECONDS",
        "CACHE_MAX_ENTRIES",
    ):
        monkeypatch.delenv(name, raising=False)


async def test_api_keeps_the_shared_http_pool_bounded(
    upstream: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    point_environment_at(monkeypatch, upstream, max_connections=2)
    app = build_app()
    body = deps(*({"name": f"pkg-{index}", "version": "1.0"} for index in range(6)))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/v1/scans", json=body)

    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert upstream.stats.requests == 24  # six packages x PyPI, OSV, and two deps.dev calls
    assert upstream.stats.peak_connections == 2


async def test_agent_tool_keeps_the_shared_http_pool_bounded(
    tmp_path: Path, upstream: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    point_environment_at(monkeypatch, upstream, max_connections=2)
    (tmp_path / "requirements.lock").write_text(pinned(6), encoding="utf-8")

    result = await _run(tmp_path, "requirements.lock")

    assert result["status"] == "complete"
    assert upstream.stats.requests == 24
    assert upstream.stats.peak_connections == 2

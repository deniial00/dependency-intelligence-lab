from __future__ import annotations

import asyncio

import pytest

from dependency_intelligence.domain import (
    CacheStatus,
    DependencyCoordinate,
    FailureKind,
    PackageMetadata,
    ScanStatus,
    ServiceName,
)
from dependency_intelligence.service import DependencyScanner

from .fakes import (
    FakeMetadataSource,
    FakeSupplyChainSource,
    FakeVulnerabilitySource,
    MemoryCache,
    StartGate,
    source_failure,
)


def scanner_with(
    metadata: FakeMetadataSource,
    vulnerabilities: FakeVulnerabilitySource,
    supply_chain: FakeSupplyChainSource,
    cache: MemoryCache | None = None,
) -> DependencyScanner:
    return DependencyScanner(
        metadata_source=metadata,
        vulnerability_source=vulnerabilities,
        supply_chain_source=supply_chain,
        cache=cache or MemoryCache(),
    )


@pytest.mark.orchestration
async def test_all_sources_failed_returns_failed_report() -> None:
    coordinate = DependencyCoordinate("demo", "1.0")
    scanner = scanner_with(
        FakeMetadataSource(failure=source_failure(ServiceName.PYPI)),
        FakeVulnerabilitySource(failure=source_failure(ServiceName.OSV)),
        FakeSupplyChainSource(failure=source_failure(ServiceName.DEPS_DEV)),
    )

    report = await scanner.scan((coordinate,))

    assert report.status is ScanStatus.FAILED
    assert report.results[0].assessment is None
    assert tuple(failure.service for failure in report.results[0].failures) == (
        ServiceName.PYPI,
        ServiceName.OSV,
        ServiceName.DEPS_DEV,
    )


@pytest.mark.orchestration
@pytest.mark.parametrize(
    ("failed_service", "expected_fields"),
    [
        (ServiceName.PYPI, ("vulnerabilities", "supply_chain")),
        (ServiceName.OSV, ("metadata", "supply_chain")),
        (ServiceName.DEPS_DEV, ("metadata", "vulnerabilities")),
    ],
)
async def test_single_source_failure_is_partial(
    failed_service: ServiceName,
    expected_fields: tuple[str, ...],
) -> None:
    metadata = FakeMetadataSource(
        failure=source_failure(ServiceName.PYPI) if failed_service is ServiceName.PYPI else None
    )
    vulnerabilities = FakeVulnerabilitySource(
        failure=source_failure(ServiceName.OSV) if failed_service is ServiceName.OSV else None
    )
    supply_chain = FakeSupplyChainSource(
        failure=source_failure(ServiceName.DEPS_DEV)
        if failed_service is ServiceName.DEPS_DEV
        else None
    )
    scanner = scanner_with(metadata, vulnerabilities, supply_chain)

    result = await scanner.assess(DependencyCoordinate("demo", "1.0"))

    assert result.assessment is not None
    assert result.failures[0].service is failed_service
    for field in expected_fields:
        assert getattr(result.assessment, field) is not None


@pytest.mark.orchestration
async def test_empty_vulnerability_tuple_is_complete_and_cached() -> None:
    cache = MemoryCache()
    scanner = scanner_with(
        FakeMetadataSource(),
        FakeVulnerabilitySource(vulnerabilities=()),
        FakeSupplyChainSource(),
        cache,
    )
    coordinate = DependencyCoordinate("demo", "1.0")

    first = await scanner.assess(coordinate)
    second = await scanner.assess(coordinate)

    assert first.complete
    assert first.assessment is not None
    assert first.assessment.vulnerabilities == ()
    assert second.cache_status is CacheStatus.HIT
    assert coordinate.cache_key in cache.entries


@pytest.mark.orchestration
async def test_plain_exception_is_unexpected_and_safe() -> None:
    class UnexpectedMetadataSource:
        service_name = ServiceName.PYPI

        async def fetch_metadata(self, coordinate: DependencyCoordinate) -> PackageMetadata:
            raise RuntimeError("secret response body")

    scanner = DependencyScanner(
        metadata_source=UnexpectedMetadataSource(),
        vulnerability_source=FakeVulnerabilitySource(),
        supply_chain_source=FakeSupplyChainSource(),
        cache=MemoryCache(),
    )

    result = await scanner.assess(DependencyCoordinate("demo", "1.0"))

    assert result.assessment is not None
    assert result.failures[0].service is ServiceName.PYPI
    assert result.failures[0].kind is FailureKind.UNEXPECTED
    assert result.failures[0].detail == "unexpected source error"
    assert "secret" not in result.failures[0].detail


@pytest.mark.orchestration
async def test_package_limit_and_order() -> None:
    class TrackingMetadataSource:
        service_name = ServiceName.PYPI
        active = 0
        maximum = 0

        async def fetch_metadata(self, coordinate: DependencyCoordinate) -> PackageMetadata:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return PackageMetadata("2.0.0", ">=3.10")

    metadata = TrackingMetadataSource()
    scanner = DependencyScanner(
        metadata_source=metadata,
        vulnerability_source=FakeVulnerabilitySource(),
        supply_chain_source=FakeSupplyChainSource(),
        cache=MemoryCache(),
        max_concurrent_packages=2,
    )
    coordinates = tuple(
        DependencyCoordinate(name, "1.0")
        for name in ("first", "second", "third", "fourth", "fifth")
    )

    report = await scanner.scan(coordinates)

    assert metadata.maximum <= 2
    assert [result.coordinate.name for result in report.results] == [
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
    ]


@pytest.mark.orchestration
async def test_cancelling_one_coalesced_waiter_keeps_load_running() -> None:
    gate = StartGate(3)
    scanner = scanner_with(
        FakeMetadataSource(gate=gate),
        FakeVulnerabilitySource(gate=gate),
        FakeSupplyChainSource(gate=gate),
    )
    coordinate = DependencyCoordinate("demo", "1.0")

    first = asyncio.create_task(scanner.assess(coordinate))
    await asyncio.sleep(0)
    second = asyncio.create_task(scanner.assess(coordinate))
    await asyncio.wait_for(gate.all_started.wait(), timeout=1)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.release.set()

    result = await second
    assert result.complete


@pytest.mark.orchestration
async def test_partial_result_is_not_cached() -> None:
    cache = MemoryCache()
    scanner = scanner_with(
        FakeMetadataSource(),
        FakeVulnerabilitySource(failure=source_failure(ServiceName.OSV)),
        FakeSupplyChainSource(),
        cache,
    )
    coordinate = DependencyCoordinate("demo", "1.0")

    first = await scanner.assess(coordinate)
    second = await scanner.assess(coordinate)

    assert first.assessment is not None
    assert first.failures
    assert first.cache_status is CacheStatus.MISS
    assert second.cache_status is CacheStatus.MISS
    assert coordinate.cache_key not in cache.entries

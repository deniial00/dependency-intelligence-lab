"""Dependency scan orchestration — student implementation seam."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .domain import (
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
from .errors import SourceRequestError
from .ports import AssessmentCache, PackageMetadataSource, SupplyChainSource, VulnerabilitySource
from .singleflight import SingleFlight


class DependencyScanner:
    def __init__(
        self,
        *,
        metadata_source: PackageMetadataSource,
        vulnerability_source: VulnerabilitySource,
        supply_chain_source: SupplyChainSource,
        cache: AssessmentCache,
        max_concurrent_packages: int = 8,
    ) -> None:
        if max_concurrent_packages <= 0:
            raise ValueError("max_concurrent_packages must be positive")
        self._metadata_source = metadata_source
        self._vulnerability_source = vulnerability_source
        self._supply_chain_source = supply_chain_source
        self._cache = cache
        self._max_concurrent_packages = max_concurrent_packages
        self._flights = SingleFlight[str, DependencyScanResult]()

    async def scan(
        self, dependencies: tuple[DependencyCoordinate, ...] | list[DependencyCoordinate]
    ) -> ProjectScanReport:
        """Implement bounded package concurrency, cancellation, and stable order."""
        ordered = tuple(dependencies)
        results: list[DependencyScanResult | None] = [None] * len(ordered)
        next_index = 0

        async def worker() -> None:
            nonlocal next_index
            while True:
                index = next_index
                if index >= len(ordered):
                    return
                next_index += 1
                results[index] = await self.assess(ordered[index])

        worker_count = min(self._max_concurrent_packages, len(ordered))
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for worker_task in workers:
                if not worker_task.done():
                    worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        return ProjectScanReport(tuple(result for result in results if result is not None))

    async def assess(self, coordinate: DependencyCoordinate) -> DependencyScanResult:
        """Implement cache/single-flight and concurrent source aggregation."""
        cached = await self._cache.get(coordinate.cache_key)
        if cached is not None:
            return DependencyScanResult(
                coordinate=coordinate,
                assessment=cached,
                failures=(),
                cache_status=CacheStatus.HIT,
            )

        result, shared = await self._flights.run(
            coordinate.cache_key,
            lambda: self._load(coordinate),
        )
        if shared:
            return replace(result, cache_status=CacheStatus.COALESCED)
        return result

    async def _load(self, coordinate: DependencyCoordinate) -> DependencyScanResult:
        metadata_task = asyncio.create_task(self._load_metadata(coordinate))
        vulnerabilities_task = asyncio.create_task(self._load_vulnerabilities(coordinate))
        supply_chain_task = asyncio.create_task(self._load_supply_chain(coordinate))
        source_tasks = (metadata_task, vulnerabilities_task, supply_chain_task)

        try:
            metadata, vulnerabilities, supply_chain = await asyncio.gather(*source_tasks)
        except asyncio.CancelledError:
            for source_task in source_tasks:
                if not source_task.done():
                    source_task.cancel()
            await asyncio.gather(*source_tasks, return_exceptions=True)
            raise

        failures = tuple(
            value
            for value in (metadata[1], vulnerabilities[1], supply_chain[1])
            if value is not None
        )
        assessment = (
            PackageAssessment(
                coordinate=coordinate,
                metadata=metadata[0],
                vulnerabilities=vulnerabilities[0],
                supply_chain=supply_chain[0],
            )
            if metadata[0] is not None
            or vulnerabilities[0] is not None
            or supply_chain[0] is not None
            else None
        )
        if assessment is not None and assessment.complete:
            await self._cache.put(coordinate.cache_key, assessment)

        return DependencyScanResult(
            coordinate=coordinate,
            assessment=assessment,
            failures=failures,
            cache_status=CacheStatus.MISS,
        )

    async def _load_metadata(
        self, coordinate: DependencyCoordinate
    ) -> tuple[PackageMetadata | None, SourceFailure | None]:
        try:
            value = await self._metadata_source.fetch_metadata(coordinate)
            return value, None
        except asyncio.CancelledError:
            raise
        except SourceRequestError as error:
            return None, error.failure
        except Exception:
            return None, self._unexpected_failure(ServiceName.PYPI)

    async def _load_vulnerabilities(
        self, coordinate: DependencyCoordinate
    ) -> tuple[tuple[Vulnerability, ...] | None, SourceFailure | None]:
        try:
            value = await self._vulnerability_source.fetch_vulnerabilities(coordinate)
            return value, None
        except asyncio.CancelledError:
            raise
        except SourceRequestError as error:
            return None, error.failure
        except Exception:
            return None, self._unexpected_failure(ServiceName.OSV)

    async def _load_supply_chain(
        self, coordinate: DependencyCoordinate
    ) -> tuple[SupplyChainInformation | None, SourceFailure | None]:
        try:
            value = await self._supply_chain_source.fetch_supply_chain(coordinate)
            return value, None
        except asyncio.CancelledError:
            raise
        except SourceRequestError as error:
            return None, error.failure
        except Exception:
            return None, self._unexpected_failure(ServiceName.DEPS_DEV)

    @staticmethod
    def _unexpected_failure(service: ServiceName) -> SourceFailure:
        return SourceFailure(
            service=service,
            kind=FailureKind.UNEXPECTED,
            retryable=False,
            detail="unexpected source error",
        )

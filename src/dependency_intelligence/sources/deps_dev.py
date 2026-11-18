"""deps.dev supply-chain adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from dependency_intelligence.domain import (
    DependencyCoordinate,
    ServiceName,
    SupplyChainInformation,
)
from dependency_intelligence.http import JsonHttpClient
from dependency_intelligence.upstream_models import DepsGraph, DepsMetadata, parse_response


class DepsDevClient:
    service_name = ServiceName.DEPS_DEV

    def __init__(self, http: JsonHttpClient, *, base_url: str = "https://api.deps.dev") -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def fetch_supply_chain(self, coordinate: DependencyCoordinate) -> SupplyChainInformation:
        """Combine version metadata and the dependency graph (SPEC 4.3).

        The two endpoints are independent, so both requests are in flight together.
        """
        version_url = (
            f"{self._base_url}/v3/systems/PYPI/packages/{quote(coordinate.name, safe='')}"
            f"/versions/{quote(coordinate.version, safe='')}"
        )
        metadata, graph = await _run_together(
            self._load(DepsMetadata, version_url),
            self._load(DepsGraph, f"{version_url}:dependencies"),
        )
        return SupplyChainInformation(
            licenses=tuple(metadata.licenses),
            direct_dependency_count=sum(1 for node in graph.nodes if node.relation == "DIRECT"),
            published_at=metadata.published_at,
        )

    async def _load[T: BaseModel](self, model: type[T], url: str) -> T:
        payload = await self._http.request_json(self.service_name, "GET", url)
        return parse_response(model, payload, self.service_name)


async def _run_together[A, B](
    first: Coroutine[Any, Any, A], second: Coroutine[Any, Any, B]
) -> tuple[A, B]:
    """Run two independent coroutines concurrently and return both results.

    The first failure cancels the other coroutine. A failure is raised as the original
    exception, never as an ``ExceptionGroup``. If both coroutines failed, the failure of
    ``first`` wins, so the outcome does not depend on completion timing.
    """
    tasks: list[asyncio.Task[Any]] = []
    failure: BaseException | None = None
    try:
        async with asyncio.TaskGroup() as group:
            tasks.append(group.create_task(first))
            tasks.append(group.create_task(second))
    except ExceptionGroup as errors:
        failure = _first_failure(tasks, errors)
    if failure is not None:
        raise failure
    return tasks[0].result(), tasks[1].result()


def _first_failure(tasks: Sequence[asyncio.Task[Any]], fallback: BaseException) -> BaseException:
    for task in tasks:
        if task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                return error
    return fallback

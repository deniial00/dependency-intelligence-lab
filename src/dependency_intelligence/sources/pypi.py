"""PyPI package-metadata adapter."""

from __future__ import annotations

from urllib.parse import quote

from dependency_intelligence.domain import DependencyCoordinate, PackageMetadata, ServiceName
from dependency_intelligence.http import JsonHttpClient
from dependency_intelligence.upstream_models import PyPIResponse, parse_response


class PyPIClient:
    service_name = ServiceName.PYPI

    def __init__(self, http: JsonHttpClient, *, base_url: str = "https://pypi.org") -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def fetch_metadata(self, coordinate: DependencyCoordinate) -> PackageMetadata:
        """Return the latest published version and its Python requirement (SPEC 4.1)."""
        url = f"{self._base_url}/pypi/{quote(coordinate.name, safe='')}/json"
        payload = await self._http.request_json(self.service_name, "GET", url)
        info = parse_response(PyPIResponse, payload, self.service_name).info
        return PackageMetadata(latest_version=info.version, requires_python=info.requires_python)

"""OSV vulnerability adapter."""

from __future__ import annotations

from dependency_intelligence.advisories import merge_advisories
from dependency_intelligence.domain import DependencyCoordinate, ServiceName, Vulnerability
from dependency_intelligence.http import JsonHttpClient
from dependency_intelligence.upstream_models import OSVResponse, parse_response


class OSVClient:
    service_name = ServiceName.OSV

    def __init__(self, http: JsonHttpClient, *, base_url: str = "https://api.osv.dev") -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def fetch_vulnerabilities(
        self, coordinate: DependencyCoordinate
    ) -> tuple[Vulnerability, ...]:
        """Return advisories for the exact coordinate, merged across aliases (SPEC 4.2).

        An omitted ``vulns`` field is a successful empty answer. ``next_page_token`` is
        not followed: the specification asks for a single query.
        """
        payload = await self._http.request_json(
            self.service_name,
            "POST",
            f"{self._base_url}/v1/query",
            json={
                "package": {"name": coordinate.name, "ecosystem": coordinate.ecosystem},
                "version": coordinate.version,
            },
        )
        response = parse_response(OSVResponse, payload, self.service_name)
        return merge_advisories(
            [
                Vulnerability(entry.id, tuple(entry.aliases), entry.summary)
                for entry in response.vulns
            ]
        )

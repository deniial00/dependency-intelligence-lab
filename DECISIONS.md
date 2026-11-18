# Engineering decisions

Record only decisions that affect behavior, interfaces, failure semantics, or
verification. “The AI suggested it” is not a rationale.

## Decision 1

- Context: SPEC 4.3 requires a deps.dev graph with exactly one `SELF` root and no
  reported errors. It does not say whether the `versionKey` in the metadata or in
  the root node must equal the requested coordinate. The supplied
  `VersionKey.coordinate()` helper exists but nothing in the specification uses it.
- Options considered: (a) reject a metadata or root `versionKey` that differs from the
  request as `invalid_response`; (b) validate only the structure SPEC 4.3 lists.
- Decision: (b). `DepsDevClient` does not compare `versionKey` values with the
  requested coordinate and does not call `VersionKey.coordinate()`.
- Consequences and risk: the request URL already pins the exact coordinate. Option (a)
  would reject responses for reasons the specification does not give, and upstream
  spellings can legitimately differ (name casing, `1.0` versus `1.0.0`). The cost is
  that data for a different coordinate, returned by a faulty proxy or upstream, is
  accepted. Revisit if the specification adds an identity requirement.
- Specification or acceptance criteria affected: SPEC 4.3, AC-02.
- Evidence that will verify it: `tests/test_sources_adapters.py`
  `test_deps_dev_does_not_compare_version_keys_with_the_requested_coordinate`, and
  `tests/test_contract.py` `test_source_adapters_translate_valid_responses`.

## Decision 2

- Context: the two deps.dev requests (metadata and `:dependencies`) run together, and
  SPEC 5 asks that outstanding work is cancelled when it is no longer needed. Either
  request can fail. If both fail, the specification does not say which failure the
  caller sees.
- Options considered: (a) fail fast: the first failure cancels the other request and is
  reported; (b) wait for both requests to settle, then report the metadata failure.
- Decision: (a). The first failure in time is reported as the original
  `SourceRequestError`, never an `ExceptionGroup`. A request cancelled before it could
  fail reports nothing. Failures that land in the same event-loop pass are ordered by
  endpoint, metadata first, so a tie does not depend on callback order.
- Consequences and risk: which failure kind is reported when both endpoints are
  failing can depend on timing. The scanner reports one deps.dev failure either way.
  Option (b) would be fully deterministic, but a slow or hung sibling would hold a
  pooled connection and delay the report until it timed out.
- Specification or acceptance criteria affected: SPEC 4.3 and 5, AC-02, AC-03.
- Evidence that will verify it: `tests/test_sources_adapters.py`
  `test_deps_dev_reports_the_first_failure_and_cancels_a_later_one`,
  `test_deps_dev_failures_in_the_same_pass_are_ordered_by_endpoint`,
  `test_deps_dev_failure_cancels_the_other_request_and_is_not_grouped`, and
  `test_cancelling_deps_dev_cancels_both_requests`.

## Decision 3

- Context: SPEC 4.2 asks for one `POST /v1/query` with the exact coordinate. The OSV API
  can answer a large result set with a `next_page_token`, which a client may send back
  to fetch the next page.
- Options considered: (a) follow `next_page_token` until it is absent; (b) make the
  single query the specification describes and ignore the token.
- Decision: (b). `OSVClient` sends one request per coordinate and does not follow
  `next_page_token`.
- Consequences and risk: if OSV ever paginates a coordinate's advisories, the result
  lists only the first page and is reported as a complete, cacheable answer, with no
  failure recorded. This was not observed with the supplied fixtures. Following
  pagination would add requests per package, which interacts with the bounded
  connection pool and request timeout, and needs a specified page limit.
- Specification or acceptance criteria affected: SPEC 4.2, AC-02.
- Evidence that will verify it: `tests/test_sources_adapters.py`
  `test_osv_makes_a_single_query_and_ignores_next_page_token`.

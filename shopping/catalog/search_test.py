#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Conformance tests for the catalog search capability (catalog/search.md).

Covers the 2 testable MUST rules of `catalog/search.md`:

  * CATALOG-SEARCH-0003: implementations MUST validate that incoming requests
    contain at least one recognized input (`query`, `filters`, or an
    extension-defined input).
  * CATALOG-SEARCH-0007: when `pagination.limit` is omitted, the Business
    MUST apply a default page size.
"""

from typing import Any

from absl.testing import absltest
from framework.decorators import requires_capability, spec_assert
from shopping.base import ShoppingIntegrationTestBase

_SEARCH = "dev.ucp.shopping.catalog.search"


def _is_rejected_search(status_code: int, body: Any) -> bool:
  """Return whether a search response rejects the request as invalid."""
  if status_code == 400:
    return True
  if status_code == 200 and isinstance(body, dict):
    ucp = body.get("ucp") or {}
    messages = body.get("messages") or []
    has_error_msg = any(
      isinstance(m, dict) and m.get("type") == "error" for m in messages
    )
    return ucp.get("status") == "error" or (
      has_error_msg and not body.get("products")
    )
  return False


@requires_capability(_SEARCH)
class CatalogSearchTest(ShoppingIntegrationTestBase):
  """Catalog Search conformance (catalog/search.md).

  Validated Paths:
  - POST /catalog/search
  """

  # ── catalog/search.md, Search Inputs ────────────────────────────────────
  @spec_assert(
    # MUST validate that requests contain at least one recognized input
    "REQ-SHOPPING-CATALOG-SEARCH-0003",
  )
  def test_empty_search_request_is_rejected(self):
    """Test that /catalog/search rejects a request with no recognized input."""
    for label, payload in (
      ("empty body {}", {}),
      ("context-only body", {"context": {"address_country": "US"}}),
    ):
      with self.subTest(case=label):
        response = self.catalog_search(payload)
        try:
          body = response.json()
        except ValueError:
          body = None
        self.assertTrue(
          _is_rejected_search(response.status_code, body),
          f"/catalog/search accepted {label} without any recognized input "
          f"(CATALOG-SEARCH-0003): HTTP {response.status_code}, body={body}",
        )

  # ── catalog/search.md, Pagination > Page Size ───────────────────────────
  @spec_assert(
    # When limit is omitted, the Business MUST apply a default page size
    "REQ-SHOPPING-CATALOG-SEARCH-0007",
  )
  def test_search_without_limit_applies_default_page_size(self):
    """Test that omitting pagination.limit applies the default page size."""
    default_limit = self.require_trigger("catalog_search_default_page_size")
    query = self.fixture_ctx.get_catalog_search_query()

    omitted_resp = self.catalog_search({"query": query})
    self.assert_response_status(omitted_resp, 200)
    omitted_body = omitted_resp.json()
    products = omitted_body.get("products")
    self.assertIsInstance(
      products,
      list,
      f"/catalog/search response lacks a products[] list: {omitted_body}",
    )
    self.assertGreaterEqual(
      len(products),
      1,
      f"/catalog/search for {query!r} returned no products to check pagination",
    )
    self.assertLessEqual(
      len(products),
      default_limit,
      f"/catalog/search with limit omitted returned {len(products)} products, "
      f"exceeding the declared default page size {default_limit} "
      "(CATALOG-SEARCH-0007)",
    )
    pagination = omitted_body.get("pagination") or {}
    if pagination.get("has_next_page"):
      self.assertEqual(
        len(products),
        default_limit,
        f"/catalog/search with has_next_page=true returned {len(products)} "
        f"products instead of the default page size {default_limit}",
      )

    explicit_resp = self.catalog_search(
      {"query": query, "pagination": {"limit": default_limit}}
    )
    self.assert_response_status(explicit_resp, 200)
    explicit_products = explicit_resp.json().get("products") or []
    self.assertEqual(
      len(products),
      len(explicit_products),
      f"omitting limit returned {len(products)} products, whereas "
      f"limit={default_limit} returned {len(explicit_products)}",
    )


if __name__ == "__main__":
  absltest.main()

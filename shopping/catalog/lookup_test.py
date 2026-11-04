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

"""Conformance tests for catalog lookup and unit pricing (catalog/lookup.md).

Covers the 16 testable MUST and MUST NOT rules of `catalog/index.md`,
`catalog/lookup.md`, and `catalog/rest.md`:

  * CATALOG-0045: when the Lookup capability is advertised, both
    `/catalog/lookup` and `/catalog/product` MUST be available.
  * CATALOG-0046: lookup requests return HTTP 200; unknown identifiers
    result in fewer products returned (with optional `not_found` info).
  * CATALOG-LOOKUP-0002: lookup MUST support product ID and variant ID.
  * CATALOG-LOOKUP-0004: duplicate identifiers in the request MUST be
    deduplicated.
  * CATALOG-LOOKUP-0006: when multiple identifiers resolve to the same
    product, it MUST be returned once.
  * CATALOG-LOOKUP-0007: variants without an `inputs` entry MUST NOT appear
    in lookup responses.
  * CATALOG-LOOKUP-0010: requests exceeding the batch limit MUST be rejected
    with HTTP 400 `request_too_large`.
  * CATALOG-LOOKUP-0012: in Get Product, when the requested ID is a variant
    ID, that variant MUST be the first element (`variants[0]`).
  * CATALOG-LOOKUP-0014: Get Product MUST include `product.selected` when the
    product has configurable options.
  * CATALOG-0003, -0004, -0005, -0007, -0008, -0009: `unit_price` invariants
    on variants.
  * CATALOG-0011: on transaction lines, the Business MUST echo `unit_price`
    on any line whose pricing basis differs from its sale basis.
"""

from typing import Any

from absl.testing import absltest
from framework.decorators import requires_capability, spec_assert
from shopping.base import ShoppingIntegrationTestBase

_LOOKUP = "dev.ucp.shopping.catalog.lookup"


def _find_variant(
  products: list[dict[str, Any]], identifier: str
) -> dict[str, Any] | None:
  """Return the variant matching `identifier` by variant ID or product ID."""
  for product in products:
    variants = product.get("variants") or []
    for variant in variants:
      if variant.get("id") == identifier:
        return variant
    if product.get("id") == identifier and variants:
      return variants[0]
  return None


def _has_error_code(body: Any, code: str) -> bool:
  """Return whether an error body carries `code` in messages or top-level."""
  if not isinstance(body, dict):
    return False
  if body.get("code") == code or body.get("error") == code:
    return True
  return any(
    isinstance(msg, dict) and msg.get("code") == code
    for msg in body.get("messages") or []
  )


@requires_capability(_LOOKUP)
class CatalogLookupTest(ShoppingIntegrationTestBase):
  """Catalog Lookup and unit-price conformance (catalog/lookup.md).

  Validated Paths:
  - POST /catalog/lookup
  - POST /catalog/product
  - POST /checkout-sessions (for CATALOG-0011 line-level unit_price echo)
  """

  # ── catalog/rest.md, Conformance ────────────────────────────────────────
  @spec_assert(
    # both /catalog/lookup and /catalog/product MUST be available
    "REQ-SHOPPING-CATALOG-0045",
  )
  def test_lookup_and_product_endpoints_are_available(self):
    """Test that both /catalog/lookup and /catalog/product respond with 200."""
    sku = self.fixture_ctx.get_test_sku()

    lookup_resp = self.catalog_lookup([sku])
    self.assert_response_status(lookup_resp, 200)
    lookup_body = lookup_resp.json()
    self.assertIsInstance(
      lookup_body.get("products"),
      list,
      f"/catalog/lookup response lacks a products[] list: {lookup_body}",
    )
    self.assertTrue(
      lookup_body["products"],
      f"/catalog/lookup returned no products for known item {sku!r}",
    )

    product_resp = self.catalog_product(sku)
    self.assert_response_status(product_resp, 200)
    product_body = product_resp.json()
    self.assertIsInstance(
      product_body.get("product"),
      dict,
      f"/catalog/product response lacks a product object: {product_body}",
    )
    self.assertTrue(
      product_body["product"].get("id"),
      f"/catalog/product returned a product without an id: {product_body}",
    )

  @spec_assert(
    # Return HTTP 200 for lookup; unknown IDs result in fewer products
    "REQ-SHOPPING-CATALOG-0046",
  )
  def test_unknown_identifiers_return_200_with_fewer_products(self):
    """Test that unknown IDs in /catalog/lookup return 200 and fewer items."""
    sku = self.fixture_ctx.get_test_sku()
    unknown = self.fixture_ctx.get_non_existent_id()

    mixed_resp = self.catalog_lookup([sku, unknown])
    self.assert_response_status(mixed_resp, 200)
    mixed_body = mixed_resp.json()
    products = mixed_body.get("products")
    self.assertIsInstance(products, list)
    self.assertLen(
      products,
      1,
      f"expected 1 product for [{sku!r}, {unknown!r}], got {products}",
    )
    for message in mixed_body.get("messages") or []:
      if isinstance(message, dict) and message.get("code") == "not_found":
        self.assertEqual(
          message.get("type"),
          "info",
          f"not_found message on lookup must be informational: {message}",
        )

    none_resp = self.catalog_lookup([unknown])
    self.assert_response_status(none_resp, 200)
    none_body = none_resp.json()
    self.assertEqual(
      none_body.get("products"),
      [],
      f"expected empty products[] for unknown ID {unknown!r}, got {none_body}",
    )

  # ── catalog/lookup.md, Batch Lookup ─────────────────────────────────────
  @spec_assert(
    # MUST support lookup by product ID and variant ID
    "REQ-SHOPPING-CATALOG-LOOKUP-0002",
  )
  def test_lookup_supports_product_and_variant_ids(self):
    """Test that /catalog/lookup resolves both a product ID and a variant ID."""
    pair = self.require_trigger("catalog_variant")
    product_id = pair["product_id"]
    variant_id = pair["variant_id"]

    by_product = self.catalog_lookup([product_id])
    self.assert_response_status(by_product, 200)
    product_list = by_product.json().get("products") or []
    self.assertEqual(
      [p.get("id") for p in product_list],
      [product_id],
      f"lookup by product ID {product_id!r} did not resolve: {product_list}",
    )

    by_variant = self.catalog_lookup([variant_id])
    self.assert_response_status(by_variant, 200)
    variant_products = by_variant.json().get("products") or []
    self.assertEqual(
      [p.get("id") for p in variant_products],
      [product_id],
      f"lookup by variant ID {variant_id!r} did not resolve to product "
      f"{product_id!r}: {variant_products}",
    )
    returned_variant_ids = [
      v.get("id") for v in variant_products[0].get("variants") or []
    ]
    self.assertIn(
      variant_id,
      returned_variant_ids,
      f"lookup by variant ID {variant_id!r} did not return that variant: "
      f"{variant_products[0]}",
    )

  @spec_assert(
    # Duplicate identifiers in the request MUST be deduplicated
    "REQ-SHOPPING-CATALOG-LOOKUP-0004",
  )
  def test_duplicate_identifiers_are_deduplicated(self):
    """Test that duplicate IDs in /catalog/lookup are deduplicated."""
    sku = self.fixture_ctx.get_test_sku()

    response = self.catalog_lookup([sku, sku, sku])
    self.assert_response_status(response, 200)
    products = response.json().get("products") or []
    self.assertLen(
      products,
      1,
      f"duplicate request IDs [{sku!r}, {sku!r}, {sku!r}] were not "
      f"deduplicated: got {len(products)} products ({products})",
    )
    for variant in products[0].get("variants") or []:
      input_ids = [
        entry.get("id")
        for entry in variant.get("inputs") or []
        if isinstance(entry, dict)
      ]
      self.assertEqual(
        input_ids.count(sku),
        1,
        f"variant {variant.get('id')!r} repeats {sku!r} in inputs[]: "
        f"{variant.get('inputs')}",
      )

  @spec_assert(
    # Multiple identifiers resolving to the same product MUST return it once
    "REQ-SHOPPING-CATALOG-LOOKUP-0006",
  )
  def test_lookup_returns_product_once_for_multiple_identifiers(self):
    """Test that product and variant IDs in one lookup return product once."""
    pair = self.require_trigger("catalog_variant")
    product_id = pair["product_id"]
    variant_id = pair["variant_id"]

    response = self.catalog_lookup([product_id, variant_id])
    self.assert_response_status(response, 200)
    products = response.json().get("products") or []
    returned_ids = [p.get("id") for p in products]
    self.assertEqual(
      returned_ids.count(product_id),
      1,
      f"product {product_id!r} was returned {returned_ids.count(product_id)} "
      f"times when both product ID {product_id!r} and variant ID "
      f"{variant_id!r} were requested: {products}",
    )

  @spec_assert(
    # Variants without an inputs entry MUST NOT appear in lookup responses
    "REQ-SHOPPING-CATALOG-LOOKUP-0007",
  )
  def test_lookup_variants_all_carry_inputs_correlation(self):
    """Test that every variant in /catalog/lookup has a non-empty inputs[]."""
    ids = [self.fixture_ctx.get_test_sku()]
    pair = self.fixture_ctx.get_catalog_variant()
    if pair is not None and pair["product_id"] not in ids:
      ids.append(pair["product_id"])

    response = self.catalog_lookup(ids)
    self.assert_response_status(response, 200)
    products = response.json().get("products") or []
    self.assertTrue(
      products, f"/catalog/lookup returned no products for {ids!r}"
    )
    for product in products:
      variants = product.get("variants") or []
      self.assertTrue(
        variants, f"product {product.get('id')!r} has no variants[]"
      )
      for variant in variants:
        inputs = variant.get("inputs")
        self.assertIsInstance(
          inputs,
          list,
          f"variant {variant.get('id')!r} of product {product.get('id')!r} "
          f"has no inputs[] array (CATALOG-LOOKUP-0007): {variant}",
        )
        self.assertTrue(
          inputs,
          f"variant {variant.get('id')!r} of product {product.get('id')!r} "
          "appeared in lookup with an empty inputs[] array "
          f"(CATALOG-LOOKUP-0007): {variant}",
        )
        for entry in inputs:
          self.assertIn(
            entry.get("id"),
            ids,
            f"inputs[] entry {entry} does not correlate to requested IDs {ids}",
          )
          self.assertIn(
            entry.get("match"),
            ("exact", "featured"),
            f"inputs[] entry {entry} has invalid match type",
          )

  @spec_assert(
    # MUST reject requests exceeding limit with HTTP 400 request_too_large
    "REQ-SHOPPING-CATALOG-LOOKUP-0010",
  )
  def test_batch_exceeding_limit_rejected_with_request_too_large(self):
    """Test that /catalog/lookup rejects oversized batches with HTTP 400."""
    limit = self.fixture_ctx.get_catalog_max_batch_size()
    oversized = [f"item_{index}" for index in range(limit + 1)]

    response = self.catalog_lookup(oversized)
    self.assertEqual(
      response.status_code,
      400,
      f"/catalog/lookup with {len(oversized)} IDs (limit {limit}) returned "
      f"HTTP {response.status_code}, expected HTTP 400 request_too_large "
      f"(CATALOG-LOOKUP-0010): {response.text[:300]}",
    )
    body = response.json()
    self.assertTrue(
      _has_error_code(body, "request_too_large"),
      f"HTTP 400 response for oversized batch lacks code 'request_too_large': "
      f"{body}",
    )

  # ── catalog/lookup.md, Get Product ──────────────────────────────────────
  @spec_assert(
    # The requested variant MUST be the first element (featured)
    "REQ-SHOPPING-CATALOG-LOOKUP-0012",
  )
  def test_get_product_by_variant_id_places_variant_first(self):
    """Test that /catalog/product for a variant ID places that variant first."""
    pair = self.require_trigger("catalog_variant")
    product_id = pair["product_id"]
    variant_id = pair["variant_id"]

    response = self.catalog_product(variant_id)
    self.assert_response_status(response, 200)
    product = response.json().get("product") or {}
    self.assertEqual(
      product.get("id"),
      product_id,
      f"/catalog/product for variant {variant_id!r} returned product "
      f"{product.get('id')!r}, expected {product_id!r}",
    )
    variants = product.get("variants") or []
    self.assertTrue(
      variants,
      f"/catalog/product for variant {variant_id!r} returned no variants",
    )
    self.assertEqual(
      variants[0].get("id"),
      variant_id,
      f"/catalog/product for variant {variant_id!r} did not place the "
      f"requested variant first (CATALOG-LOOKUP-0012); variants: "
      f"{[v.get('id') for v in variants]}",
    )

  @spec_assert(
    # MUST include product.selected when product has configurable options
    "REQ-SHOPPING-CATALOG-LOOKUP-0014",
  )
  def test_get_product_includes_effective_selected_options(self):
    """Test that /catalog/product includes product.selected for options."""
    target = self.require_trigger("catalog_product_with_options")
    product_id = target["id"]
    requested_selected = target["selected"]

    # 1. Without `selected` in the request: product.selected reflects the
    # featured variant's default selections.
    default_resp = self.catalog_product(product_id)
    self.assert_response_status(default_resp, 200)
    default_product = default_resp.json().get("product") or {}
    self.assertTrue(
      default_product.get("options"),
      f"fixture product {product_id!r} has no configurable options[]: "
      f"{default_product}",
    )
    default_selected = default_product.get("selected")
    self.assertIsInstance(
      default_selected,
      list,
      f"/catalog/product for {product_id!r} omitted product.selected "
      f"(CATALOG-LOOKUP-0014): {default_product}",
    )
    self.assertTrue(
      default_selected,
      f"product.selected is empty for configurable product {product_id!r}",
    )
    featured_options = (default_product.get("variants") or [{}])[0].get(
      "options"
    )
    if featured_options:
      self.assertEqual(
        {(opt.get("name"), opt.get("label")) for opt in default_selected},
        {(opt.get("name"), opt.get("label")) for opt in featured_options},
        "default product.selected does not match the featured variant's "
        f"options: selected={default_selected}, featured={featured_options}",
      )

    # 2. With `selected` in the request: product.selected reflects the
    # effective selections.
    narrowed_resp = self.catalog_product(
      product_id, selected=requested_selected
    )
    self.assert_response_status(narrowed_resp, 200)
    narrowed_product = narrowed_resp.json().get("product") or {}
    narrowed_selected = narrowed_product.get("selected")
    self.assertIsInstance(
      narrowed_selected,
      list,
      f"/catalog/product with selected={requested_selected} omitted "
      f"product.selected (CATALOG-LOOKUP-0014): {narrowed_product}",
    )
    self.assertTrue(
      narrowed_selected,
      f"product.selected is empty when selected={requested_selected} was sent",
    )
    effective_pairs = {
      (opt.get("name"), opt.get("label")) for opt in narrowed_selected
    }
    for choice in requested_selected:
      self.assertIn(
        (choice["name"], choice["label"]),
        effective_pairs,
        f"requested selection {choice} not reflected in product.selected "
        f"{narrowed_selected}",
      )

  # ── catalog/index.md, Distinction from unit price ───────────────────────
  @spec_assert(
    "REQ-SHOPPING-CATALOG-0003",  # positive integers for measure and reference
    "REQ-SHOPPING-CATALOG-0004",  # unit_price.currency == price.currency
    "REQ-SHOPPING-CATALOG-0005",  # measure.unit == reference.unit
    "REQ-SHOPPING-CATALOG-0007",  # no cross-unit or currency conversion
    "REQ-SHOPPING-CATALOG-0008",  # comparator formula from price and values
    "REQ-SHOPPING-CATALOG-0009",  # round once to minor units as amount
  )
  def test_variant_unit_price_invariants(self):
    """Test that a variant's unit_price satisfies CATALOG-0003..0009."""
    sku = self.require_trigger("unit_price_item_sku")

    lookup_resp = self.catalog_lookup([sku])
    self.assert_response_status(lookup_resp, 200)
    variant = _find_variant(lookup_resp.json().get("products") or [], sku)
    self.assertIsNotNone(
      variant,
      f"/catalog/lookup returned no variant for unit_price_item_sku {sku!r}",
    )
    assert variant is not None
    self.assertIn(
      "unit_price",
      variant,
      f"variant {variant.get('id')!r} in /catalog/lookup lacks unit_price: "
      f"{variant}",
    )
    self.assert_unit_price(
      variant["unit_price"],
      variant.get("price"),
      where=f"lookup variant[{sku!r}].unit_price",
    )

    product_resp = self.catalog_product(sku)
    self.assert_response_status(product_resp, 200)
    product = product_resp.json().get("product") or {}
    detail_variant = _find_variant([product], sku)
    self.assertIsNotNone(
      detail_variant,
      f"/catalog/product returned no variant for unit_price_item_sku {sku!r}",
    )
    assert detail_variant is not None
    self.assertIn(
      "unit_price",
      detail_variant,
      f"variant {detail_variant.get('id')!r} in /catalog/product carries no "
      f"unit_price: {detail_variant}",
    )
    self.assert_unit_price(
      detail_variant["unit_price"],
      detail_variant.get("price"),
      where=f"product variant[{sku!r}].unit_price",
    )

  @spec_assert(
    # MUST echo unit_price on lines where pricing basis differs from sale basis
    "REQ-SHOPPING-CATALOG-0011",
  )
  def test_line_item_echoes_unit_price_when_pricing_basis_differs(self):
    """Test that checkout echoes item.unit_price when pricing basis differs."""
    sku = self.require_trigger("unit_price_item_sku")

    lookup_resp = self.catalog_lookup([sku])
    self.assert_response_status(lookup_resp, 200)
    catalog_variant = _find_variant(
      lookup_resp.json().get("products") or [], sku
    )
    self.assertIsNotNone(
      catalog_variant,
      f"/catalog/lookup returned no variant for unit_price_item_sku {sku!r}",
    )
    assert catalog_variant is not None
    catalog_up = catalog_variant.get("unit_price")
    self.assertIsInstance(
      catalog_up,
      dict,
      f"catalog variant {sku!r} carries no unit_price: {catalog_variant}",
    )

    checkout_data = self.create_checkout_session(
      item_id=sku, select_fulfillment=False
    )
    lines = checkout_data.get("line_items") or []
    self.assertTrue(lines, f"checkout has no line_items: {checkout_data}")
    item = lines[0].get("item") or {}
    line_up = item.get("unit_price")
    self.assertIsInstance(
      line_up,
      dict,
      f"checkout line item for {sku!r} did not echo item.unit_price "
      f"(CATALOG-0011): {item}",
    )
    self.assert_unit_price(
      line_up,
      {"amount": item.get("price"), "currency": checkout_data.get("currency")},
      where=f"checkout line_items[0].item.unit_price ({sku!r})",
    )
    self.assertEqual(
      (line_up.get("reference") or {}).get("unit"),
      (catalog_up.get("reference") or {}).get("unit"),
      "checkout line item.unit_price.reference.unit does not match catalog's",
    )


if __name__ == "__main__":
  absltest.main()

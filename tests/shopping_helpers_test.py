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

"""Unit tests for the helpers behind the shopping suite's rule checks.

The helpers read raw JSON as the server returned it, so each test builds a
small response by hand. The assertion helpers only use assertion methods,
so they are exercised unbound with a plain ``TestCase`` as ``self``; no
server or fixture is needed.
"""

import base64
import unittest

from shopping import base
from shopping.cart import cart_test
from shopping.catalog import lookup_test, search_test
from shopping.order import webhook_structure_test as webhook
from shopping.validation import validation_test


def line(
  price: int,
  quantity: int,
  total: int | None,
  scale: int | None = None,
  total_type: str = "subtotal",
) -> dict:
  """Build a response line with the given price, quantity and total."""
  item = {"id": "item_1", "title": "Item", "price": price}
  if scale is not None:
    item["quantity_unit"] = {"unit": "kg", "scale": scale}
  built = {"id": "li_1", "item": item, "quantity": quantity}
  if total is not None:
    built["totals"] = [{"type": total_type, "amount": total}]
  return built


class ExpectedLineTotalTest(unittest.TestCase):
  """expected_line_total: price × quantity × 10^-scale, and the scale."""

  def test_whole_items_multiply_exactly(self) -> None:
    """Scale 0: the product is exact."""
    self.assertEqual(base.expected_line_total(line(1999, 3, 5997)), (5997, 0))

  def test_absent_descriptor_means_scale_zero(self) -> None:
    """No quantity_unit on the item reads as scale 0."""
    _, scale = base.expected_line_total(line(100, 1, 100))
    self.assertEqual(scale, 0)

  def test_measured_line_divides_by_the_scale(self) -> None:
    """A measured line's product is divided by 10^scale, unrounded."""
    # 2.5 kg at 4.99 per kg: 499 × 2500 × 10^-3 = 1247.5 before rounding.
    self.assertEqual(
      base.expected_line_total(line(499, 2500, 1248, scale=3)), (1247.5, 3)
    )


class LineTotalEntryTest(unittest.TestCase):
  """line_total_entry: the subtotal entry, or a lone total."""

  def test_prefers_the_subtotal_entry(self) -> None:
    """With both entries present the subtotal is the line's product."""
    built = line(100, 2, 200)
    built["totals"].append({"type": "total", "amount": 230})
    self.assertEqual(base.line_total_entry(built), 200)

  def test_accepts_a_lone_total(self) -> None:
    """A line whose only entry is total is read as that total."""
    self.assertEqual(
      base.line_total_entry(line(100, 2, 200, total_type="total")), 200
    )

  def test_no_subtotal_among_several_entries_is_none(self) -> None:
    """Several entries and no subtotal leave the product unknown."""
    built = line(100, 2, 200, total_type="total")
    built["totals"].append({"type": "tax", "amount": 30})
    self.assertIsNone(base.line_total_entry(built))

  def test_no_totals_is_none(self) -> None:
    """A line without totals has no entry to read."""
    self.assertIsNone(base.line_total_entry(line(100, 2, None)))


class AssertLineTotalsTest(unittest.TestCase):
  """assert_line_totals: exact for whole items, one rounding when measured."""

  def check(self, *lines: dict) -> None:
    """Run the assertion against a checkout holding the given lines."""
    base.ShoppingIntegrationTestBase.assert_line_totals(
      self, {"line_items": list(lines)}
    )

  def test_passes_when_every_line_multiplies_out(self) -> None:
    """Whole-item lines whose subtotal is price × quantity pass."""
    self.check(line(1999, 3, 5997), line(500, 1, 500))

  def test_fails_a_whole_item_line_that_is_off_by_one(self) -> None:
    """A whole-item line one minor unit off fails, naming the product."""
    with self.assertRaises(AssertionError) as caught:
      self.check(line(1999, 3, 5998))
    self.assertIn("1999 × 3 = 5997", str(caught.exception))

  def test_accepts_one_rounding_on_a_measured_line(self) -> None:
    """A measured line may round its exact product either way."""
    self.check(line(499, 2500, 1248, scale=3))
    self.check(line(499, 2500, 1247, scale=3))

  def test_fails_a_measured_line_rounded_per_unit(self) -> None:
    """A measured line more than a minor unit off fails."""
    # 999 g at 1.00 per kg: 100 × 999 × 10^-3 = 99.9; rounding each of
    # three 333 g units to 33 first gives 99, 98 is further off still.
    with self.assertRaises(AssertionError):
      self.check(line(100, 999, 98, scale=3))

  def test_fails_a_line_without_a_subtotal(self) -> None:
    """A line with no subtotal entry fails, saying so."""
    with self.assertRaises(AssertionError) as caught:
      self.check(line(100, 1, None))
    self.assertIn("no subtotal entry", str(caught.exception))

  def test_fails_an_empty_checkout(self) -> None:
    """A checkout without lines has nothing to check and fails."""
    with self.assertRaises(AssertionError):
      self.check()


class OrderCarriesCheckoutTest(unittest.TestCase):
  """assert_order_carries_checkout: the placed checkout's content."""

  CHECKOUT = {
    "id": "chk_1",
    "currency": "USD",
    "line_items": [
      {"id": "li_1", "item": {"id": "roses", "price": 3500}, "quantity": 2},
    ],
    "totals": [
      {"type": "subtotal", "amount": 7000},
      {"type": "total", "amount": 7500},
    ],
  }
  ORDER = {
    "id": "ord_1",
    "checkout_id": "chk_1",
    "currency": "USD",
    "line_items": [
      {
        "id": "oli_1",
        "item": {"id": "roses", "price": 3500},
        "quantity": {"total": 2, "fulfilled": 0},
      },
    ],
    "totals": [
      {"type": "subtotal", "amount": 7000},
      {"type": "fulfillment", "amount": 500},
      {"type": "total", "amount": 7500},
    ],
  }

  def check(self, order_data: dict) -> None:
    """Run the assertion unbound against the fixture checkout."""
    base.ShoppingIntegrationTestBase.assert_order_carries_checkout(
      self, order_data, self.CHECKOUT
    )

  def test_passes_for_the_placed_checkout(self) -> None:
    """An order with the checkout's lines and total passes."""
    self.check(self.ORDER)

  def test_accepts_a_plain_integer_quantity(self) -> None:
    """A line whose quantity is a bare integer is read as its total."""
    order_data = dict(self.ORDER)
    order_data["line_items"] = [dict(self.ORDER["line_items"][0])]
    order_data["line_items"][0]["quantity"] = 2
    self.check(order_data)

  def test_fails_a_reference_or_trimmed_order(self) -> None:
    """An order missing the checkout's content fails, naming the gap."""
    with self.assertRaises(AssertionError) as caught:
      self.check({"id": "ord_1", "permalink_url": "https://x/o/1"})
    self.assertIn("does not name the checkout", str(caught.exception))
    trimmed = dict(self.ORDER)
    trimmed["line_items"] = []
    with self.assertRaises(AssertionError) as caught:
      self.check(trimmed)
    self.assertIn(
      "no line for the checkout's item 'roses'", str(caught.exception)
    )

  def test_fails_a_different_quantity_or_total(self) -> None:
    """A line quantity or a total unlike the checkout's fails."""
    changed = dict(self.ORDER)
    changed["line_items"] = [dict(self.ORDER["line_items"][0])]
    changed["line_items"][0]["quantity"] = {"total": 1, "fulfilled": 0}
    with self.assertRaises(AssertionError):
      self.check(changed)
    changed = dict(self.ORDER)
    changed["totals"] = [{"type": "total", "amount": 7000}]
    with self.assertRaises(AssertionError):
      self.check(changed)


class SnapshotDifferencesTest(unittest.TestCase):
  """snapshot_differences: member-for-member, ignoring empty members."""

  ORDER = {
    "id": "ord_1",
    "checkout_id": "chk_1",
    "line_items": [{"id": "li_1", "quantity": 2}],
    "fulfillment": {"events": []},
    "messages": None,
  }

  def test_same_snapshot_has_no_differences(self) -> None:
    """An entity compared with itself has no differences."""
    self.assertEqual(base.snapshot_differences(self.ORDER, self.ORDER), [])

  def test_empty_and_absent_members_are_the_same(self) -> None:
    """Null, [], {} and an omitted member all read as absent."""
    delivered = dict(self.ORDER)
    del delivered["messages"]
    delivered["fulfillment"] = {}
    delivered["adjustments"] = []
    self.assertEqual(base.snapshot_differences(self.ORDER, delivered), [])

  def test_a_changed_value_is_reported_with_its_path(self) -> None:
    """A differing leaf is reported at its JSON path with both values."""
    delivered = dict(self.ORDER)
    delivered["line_items"] = [{"id": "li_1", "quantity": 1}]
    self.assertEqual(
      base.snapshot_differences(self.ORDER, delivered),
      ["$.line_items[0].quantity: 1, Get Order has 2"],
    )

  def test_missing_and_extra_members_are_reported(self) -> None:
    """Members only one side has are reported as missing or extra."""
    delivered = dict(self.ORDER)
    del delivered["checkout_id"]
    delivered["label"] = "ORDER-1"
    self.assertEqual(
      base.snapshot_differences(self.ORDER, delivered),
      [
        "$.checkout_id: missing (Get Order has 'chk_1')",
        "$.label: extra ('ORDER-1'; Get Order has none)",
      ],
    )

  def test_list_length_is_reported_once(self) -> None:
    """Lists of different lengths are one difference, not one per entry."""
    delivered = dict(self.ORDER)
    delivered["line_items"] = []
    self.assertEqual(
      base.snapshot_differences(self.ORDER, delivered),
      ["$.line_items: missing (Get Order has [{'id': 'li_1', 'quantity': 2}])"],
    )
    delivered["line_items"] = self.ORDER["line_items"] * 2
    self.assertEqual(
      base.snapshot_differences(self.ORDER, delivered),
      ["$.line_items: 2 entries, Get Order has 1"],
    )


class SignatureShapeTest(unittest.TestCase):
  """The RFC 9421 dictionary shape the ORDER-0022 test requires."""

  WEBHOOK_INPUT = (
    'sig1=("@method" "@authority" "@path" "ucp-agent" "content-digest" '
    '"content-type" "webhook-id" "webhook-timestamp");keyid="merchant-2026"'
  )
  WBA_INPUT = (
    'sig1=("@method" "@authority" "@path" "signature-agent";key="sig1" '
    '"ucp-agent" "content-digest" "content-type");'
    'keyid="poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U";created=1738617600;'
    'expires=1738621200;tag="web-bot-auth"'
  )
  SIGNATURE = "sig1=:" + base64.b64encode(b"s" * 64).decode() + ":"

  def shape(self, signature_input: str, signature: str) -> None:
    """Run the shape assertion unbound, with this case as the test."""
    webhook.WebhookStructureTest._assert_signature_shape(
      self, signature_input, signature
    )

  def test_accepts_the_spec_examples(self) -> None:
    """order.md's webhook example and signatures.md's WBA shape pass."""
    self.shape(self.WEBHOOK_INPUT, self.SIGNATURE)
    self.shape(self.WBA_INPUT, self.SIGNATURE)

  def test_rejects_malformed_headers(self) -> None:
    """Each departure from the dictionary shape fails the assertion."""
    for reason, signature_input, signature in (
      ("no keyid", 'sig1=("@method");created=1', self.SIGNATURE),
      ("empty components", 'sig1=();keyid="k"', self.SIGNATURE),
      ("unquoted component", 'sig1=(@method);keyid="k"', self.SIGNATURE),
      ("label mismatch", self.WEBHOOK_INPUT, "sig2=:QUJD:"),
      ("bad base64", self.WEBHOOK_INPUT, "sig1=:QUJ:"),
      ("empty signature", self.WEBHOOK_INPUT, "sig1=::"),
      ("not a dictionary", "garbage", self.SIGNATURE),
    ):
      with self.subTest(reason), self.assertRaises(AssertionError):
        self.shape(signature_input, signature)


class ExplainsLineTest(unittest.TestCase):
  """_explains_line: a message about a line or its quantity."""

  def test_path_into_line_items(self) -> None:
    """A path into line_items explains, whatever the code says."""
    self.assertTrue(
      validation_test._explains_line(
        {"type": "info", "code": "updated", "path": "$.line_items[0]"}
      )
    )

  def test_quantity_or_stock_in_code_or_content(self) -> None:
    """Quantity or stock in the code or content explains, any case."""
    self.assertTrue(
      validation_test._explains_line({"code": "quantity_adjusted"})
    )
    self.assertTrue(
      validation_test._explains_line(
        {"code": "item_unavailable", "content": "Only 5 left in STOCK"}
      )
    )

  def test_unrelated_message_does_not_count(self) -> None:
    """A message about something else is not the explanation."""
    self.assertFalse(
      validation_test._explains_line(
        {"type": "info", "code": "promo", "content": "Free shipping"}
      )
    )


class CartHelpersTest(unittest.TestCase):
  """The cart suite's helpers: contents by bag, state, and naming a code."""

  def test_line_item_bag_ignores_line_ids_and_order(self) -> None:
    """Two entities with the same items and quantities have the same bag."""
    cart = {
      "line_items": [
        {"id": "c1", "item": {"id": "roses"}, "quantity": 2},
        {"id": "c2", "item": {"id": "lilies"}, "quantity": 1},
      ]
    }
    checkout = {
      "line_items": [
        {"id": "x9", "item": {"id": "lilies"}, "quantity": 1},
        {"id": "x8", "item": {"id": "roses"}, "quantity": 2},
      ]
    }
    bag = base.ShoppingIntegrationTestBase.line_item_bag
    self.assertEqual(bag(cart), bag(checkout))
    checkout["line_items"][1]["quantity"] = 9
    self.assertNotEqual(bag(cart), bag(checkout))

  def test_cart_state_keeps_only_state_members(self) -> None:
    """Envelope and bookkeeping members are left out of the state."""
    state = cart_test._cart_state(
      {
        "ucp": {"version": "x"},
        "id": "cart_1",
        "line_items": [],
        "currency": "USD",
        "totals": [],
        "messages": [{"type": "info"}],
        "expires_at": "2026-01-01T00:00:00Z",
      }
    )
    self.assertEqual(
      set(state),
      {"line_items", "currency", "totals", "buyer", "context", "discounts"},
    )
    self.assertNotIn("messages", state)

  def test_names_code_in_codes_or_applied_any_case(self) -> None:
    """The code counts when submitted or applied, case-insensitively."""
    self.assertTrue(
      cart_test._names_code({"discounts": {"codes": ["save10"]}}, "SAVE10")
    )
    self.assertTrue(
      cart_test._names_code(
        {"discounts": {"applied": [{"code": "SAVE10", "amount": 100}]}},
        "save10",
      )
    )
    self.assertFalse(
      cart_test._names_code(
        {"discounts": {"applied": [{"title": "Auto", "amount": 100}]}},
        "SAVE10",
      )
    )
    self.assertFalse(cart_test._names_code({}, "SAVE10"))

  def test_message_names_code(self) -> None:
    """A rejection message mentioning the code counts; others don't."""
    self.assertTrue(
      cart_test._message_names_code(
        {
          "messages": [
            {
              "type": "warning",
              "code": "discount_code_invalid",
              "content": "Code SAVE10 has expired",
            }
          ]
        },
        "save10",
      )
    )
    self.assertFalse(
      cart_test._message_names_code(
        {"messages": [{"type": "info", "content": "Free shipping"}]}, "SAVE10"
      )
    )


class UnitPriceProblemsTest(unittest.TestCase):
  """unit_price_problems and assert_unit_price: CATALOG-0003..0009."""

  def check(self, unit_price: dict, price: dict) -> None:
    """Run assert_unit_price unbound with this TestCase as self."""
    base.ShoppingIntegrationTestBase.assert_unit_price(self, unit_price, price)

  def test_spec_cable_spool_example_passes(self) -> None:
    """A 50 m cable spool at $25.00 with a $0.50/m comparator passes."""
    self.check(
      {
        "amount": 50,
        "currency": "USD",
        "measure": {
          "value": 5000,
          "unit": "MTR",
          "scale": 2,
          "display_text": "m",
        },
        "reference": {"value": 1, "unit": "MTR", "display_text": "m"},
      },
      {"amount": 2500, "currency": "USD"},
    )

  def test_accepts_one_rounding_when_comparator_is_fractional(self) -> None:
    """750 mL at $10.00 -> per 100 mL is 133.333... minor units (133 or 134)."""
    for rounded in (133, 134):
      self.check(
        {
          "amount": rounded,
          "currency": "USD",
          "measure": {"value": 750, "unit": "MLT", "display_text": "mL"},
          "reference": {"value": 100, "unit": "MLT", "display_text": "mL"},
        },
        {"amount": 1000, "currency": "USD"},
      )

  def test_rejects_non_positive_or_non_integer_measure_values(self) -> None:
    """CATALOG-0003: measure.value and reference.value must be positive ints."""
    for bad_m, bad_r in ((0, 1), (1, -1), (True, 1), (1.5, 1)):
      problems = base.unit_price_problems(
        {
          "amount": 100,
          "currency": "USD",
          "measure": {"value": bad_m, "unit": "MTR"},
          "reference": {"value": bad_r, "unit": "MTR"},
        },
        {"amount": 100, "currency": "USD"},
      )
      self.assertTrue(
        any("CATALOG-0003" in p for p in problems),
        f"expected CATALOG-0003 violation for ({bad_m!r}, {bad_r!r}): "
        f"{problems}",
      )

  def test_rejects_currency_mismatch(self) -> None:
    """CATALOG-0004 & -0007: unit_price.currency must equal price.currency."""
    problems = base.unit_price_problems(
      {
        "amount": 100,
        "currency": "EUR",
        "measure": {"value": 1, "unit": "MTR"},
        "reference": {"value": 1, "unit": "MTR"},
      },
      {"amount": 100, "currency": "USD"},
    )
    self.assertTrue(any("CATALOG-0004" in p for p in problems))

  def test_rejects_cross_unit_comparison(self) -> None:
    """CATALOG-0005 & -0007: measure.unit must equal reference.unit."""
    problems = base.unit_price_problems(
      {
        "amount": 100,
        "currency": "USD",
        "measure": {"value": 1000, "unit": "GRM"},
        "reference": {"value": 1, "unit": "KGM"},
      },
      {"amount": 100, "currency": "USD"},
    )
    self.assertTrue(any("CATALOG-0005" in p for p in problems))

  def test_rejects_wrong_comparator_amount(self) -> None:
    """CATALOG-0008 & -0009: amount outside floor/ceil of formula fails."""
    with self.assertRaises(AssertionError) as caught:
      self.check(
        {
          "amount": 135,
          "currency": "USD",
          "measure": {"value": 750, "unit": "MLT"},
          "reference": {"value": 100, "unit": "MLT"},
        },
        {"amount": 1000, "currency": "USD"},
      )
    self.assertIn("CATALOG-0008", str(caught.exception))


class CatalogHelpersTest(unittest.TestCase):
  """Helpers used by lookup_test and search_test."""

  def test_find_variant_matches_variant_id_then_product_id(self) -> None:
    """_find_variant finds an exact variant ID or falls back to product ID."""
    products = [
      {
        "id": "prod_1",
        "variants": [{"id": "var_1a"}, {"id": "var_1b"}],
      }
    ]
    self.assertEqual(
      lookup_test._find_variant(products, "var_1b"), {"id": "var_1b"}
    )
    self.assertEqual(
      lookup_test._find_variant(products, "prod_1"), {"id": "var_1a"}
    )
    self.assertIsNone(lookup_test._find_variant(products, "missing"))

  def test_has_error_code_checks_messages_and_top_level(self) -> None:
    """_has_error_code matches messages[].code or top-level code/error."""
    self.assertTrue(
      lookup_test._has_error_code(
        {"messages": [{"type": "error", "code": "request_too_large"}]},
        "request_too_large",
      )
    )
    self.assertTrue(
      lookup_test._has_error_code(
        {"code": "request_too_large"}, "request_too_large"
      )
    )
    self.assertFalse(
      lookup_test._has_error_code(
        {"messages": [{"type": "error", "code": "other"}]},
        "request_too_large",
      )
    )

  def test_is_rejected_search_accepts_400_or_ucp_error_envelope(self) -> None:
    """_is_rejected_search recognizes HTTP 400 and UCP error envelopes."""
    self.assertTrue(search_test._is_rejected_search(400, {}))
    self.assertTrue(
      search_test._is_rejected_search(
        200,
        {
          "ucp": {"status": "error"},
          "messages": [{"type": "error", "code": "invalid_input"}],
        },
      )
    )
    self.assertFalse(
      search_test._is_rejected_search(
        200,
        {"ucp": {"status": "success"}, "products": []},
      )
    )


class EligibilityAndProvisionalDiscountHelpersTest(unittest.TestCase):
  """W7 helpers: provisional_discount_problems and eligibility_invalid_error."""

  def test_valid_provisional_discount_passes(self) -> None:
    """Provisional discount with eligibility, automatic, and negative total."""
    checkout_data = {
      "discounts": {
        "applied": [
          {
            "title": "Gold Member 15% Off",
            "amount": 1500,
            "automatic": True,
            "provisional": True,
            "eligibility": "com.example.loyalty_gold",
          }
        ]
      },
      "totals": [
        {"type": "subtotal", "amount": 10000},
        {"type": "discount", "amount": -1500},
        {"type": "total", "amount": 8500},
      ],
    }
    self.assertEqual(
      base.provisional_discount_problems(
        checkout_data, "com.example.loyalty_gold"
      ),
      [],
    )

  def test_provisional_discount_detects_missing_or_invalid_fields(self) -> None:
    """Missing provisional=True, automatic=True, or negative total fails."""
    missing = {"discounts": {"applied": []}, "totals": []}
    problems = base.provisional_discount_problems(
      missing, "com.example.loyalty_gold"
    )
    self.assertTrue(any("DISCOUNT-0006" in p for p in problems))

    bad_entry = {
      "discounts": {
        "applied": [
          {
            "title": "",
            "amount": 0,
            "code": "SHOULD_NOT_BE_SET",
            "automatic": False,
            "provisional": False,
            "eligibility": "com.example.loyalty_gold",
          }
        ]
      },
      "totals": [{"type": "total", "amount": 10000}],
    }
    problems = base.provisional_discount_problems(
      bad_entry, "com.example.loyalty_gold"
    )
    self.assertTrue(any(".provisional: must be True" in p for p in problems))
    self.assertTrue(any(".automatic: must be True" in p for p in problems))
    self.assertTrue(any(".code: must be omitted" in p for p in problems))
    self.assertTrue(any("negative discount" in p for p in problems))

  def test_valid_eligibility_invalid_error_passes(self) -> None:
    """Unverified claim at completion returns recoverable error."""
    body = {
      "id": "chk_1",
      "status": "requires_escalation",
      "messages": [
        {
          "type": "error",
          "code": "eligibility_invalid",
          "severity": "recoverable",
          "content": "Unable to verify student status.",
        }
      ],
    }
    self.assertEqual(base.eligibility_invalid_error_problems(body), [])

  def test_eligibility_invalid_error_detects_completed_or_bad_severity(
    self,
  ) -> None:
    """Completed status, order creation, or wrong severity is flagged."""
    completed_body = {
      "id": "chk_1",
      "status": "completed",
      "order": {"id": "ord_1"},
      "messages": [],
    }
    problems = base.eligibility_invalid_error_problems(completed_body)
    self.assertTrue(any("CHECKOUT-0052" in p for p in problems))
    self.assertTrue(any("CHECKOUT-0051" in p for p in problems))

    wrong_severity = {
      "id": "chk_1",
      "status": "incomplete",
      "messages": [
        {
          "type": "error",
          "code": "eligibility_invalid",
          "severity": "unrecoverable",
        }
      ],
    }
    problems = base.eligibility_invalid_error_problems(wrong_severity)
    self.assertTrue(any("CHECKOUT-0053" in p for p in problems))


class FulfillmentSelectionAndDescriptionHelpersTest(unittest.TestCase):
  """W8 helpers: selected_destination, rejected update, option description."""

  def test_selected_destination_shipping_and_business_location(self) -> None:
    """Valid shipping and business_location selections pass."""
    shipping_method = {
      "id": "method_1",
      "type": "shipping",
      "selected_destination_id": "dest_1",
      "destinations": [
        {"id": "dest_1", "type": "shipping_address", "postal_code": "94105"},
        {"id": "dest_2", "type": "shipping_address", "postal_code": "10001"},
      ],
    }
    self.assertEqual(
      base.selected_destination_problems(
        shipping_method,
        expected_id="dest_1",
        expected_type="shipping_address",
      ),
      [],
    )

    pickup_method = {
      "id": "method_pickup_1",
      "type": "pickup",
      "selected_destination_id": "store_sf_01",
      "destinations": [
        {
          "id": "store_sf_01",
          "type": "business_location",
          "name": "Market Street Flagship",
        }
      ],
    }
    self.assertEqual(
      base.selected_destination_problems(
        pickup_method,
        expected_id="store_sf_01",
        expected_type="business_location",
      ),
      [],
    )

  def test_selected_destination_detects_duplicates_or_missing_type_or_name(
    self,
  ) -> None:
    """Duplicate matching IDs, missing type, or missing pickup name fail."""
    dup_dests = {
      "selected_destination_id": "dest_1",
      "destinations": [
        {"id": "dest_1", "type": "shipping_address"},
        {"id": "dest_1", "type": "shipping_address"},
      ],
    }
    problems = base.selected_destination_problems(dup_dests)
    self.assertTrue(any("FULFILLMENT-0013" in p for p in problems))

    untyped_dest = {
      "selected_destination_id": "dest_1",
      "destinations": [{"id": "dest_1"}],
    }
    problems = base.selected_destination_problems(untyped_dest)
    self.assertTrue(any("FULFILLMENT-0013" in p for p in problems))

    nameless_pickup = {
      "selected_destination_id": "store_sf_01",
      "destinations": [{"id": "store_sf_01", "type": "business_location"}],
    }
    problems = base.selected_destination_problems(
      nameless_pickup, expected_type="business_location"
    )
    self.assertTrue(any("FULFILLMENT-0009" in p for p in problems))

  def test_rejected_destination_update_passes_when_unchanged_with_error(
    self,
  ) -> None:
    """Unchanged state + recoverable error on selected_destination_id passes."""
    before = {
      "fulfillment": {
        "methods": [
          {
            "id": "method_1",
            "selected_destination_id": "dest_1",
            "destinations": [{"id": "dest_1", "type": "shipping_address"}],
          }
        ]
      },
      "totals": [{"type": "total", "amount": 4000}],
    }
    after = {
      "fulfillment": {
        "methods": [
          {
            "id": "method_1",
            "selected_destination_id": "dest_1",
            "destinations": [{"id": "dest_1", "type": "shipping_address"}],
          }
        ]
      },
      "totals": [{"type": "total", "amount": 4000}],
      "messages": [
        {
          "type": "error",
          "code": "destination_invalid",
          "severity": "recoverable",
          "path": "$.fulfillment.methods[0].selected_destination_id",
          "content": "Destination not recognized.",
        }
      ],
    }
    self.assertEqual(
      base.rejected_destination_update_problems(before, after, method_index=0),
      [],
    )

  def test_rejected_destination_update_detects_state_mutation_or_missing_msg(
    self,
  ) -> None:
    """Mutated selected_destination_id or missing recoverable error fails."""
    before = {
      "fulfillment": {
        "methods": [
          {
            "id": "method_1",
            "selected_destination_id": "dest_1",
            "destinations": [{"id": "dest_1", "type": "shipping_address"}],
          }
        ]
      },
      "totals": [{"type": "total", "amount": 4000}],
    }
    mutated = {
      "fulfillment": {
        "methods": [
          {
            "id": "method_1",
            "selected_destination_id": "bad_dest",
            "destinations": [{"id": "dest_1", "type": "shipping_address"}],
          }
        ]
      },
      "totals": [{"type": "total", "amount": 3500}],
      "messages": [],
    }
    problems = base.rejected_destination_update_problems(before, mutated)
    self.assertTrue(any("FULFILLMENT-0016" in p for p in problems))
    self.assertTrue(any("FULFILLMENT-0017" in p for p in problems))

  def test_option_description_problems_checks_title_and_total(self) -> None:
    """Supplementary description passes; repeating title or total fails."""
    good_option = {
      "id": "std-ship",
      "title": "Standard Shipping",
      "description": {"plain": "Arrives in 3-5 business days via USPS"},
      "totals": [{"type": "total", "amount": 599}],
    }
    self.assertEqual(base.option_description_problems(good_option), [])

    repeats_title = {
      "id": "std-ship",
      "title": "Standard Shipping",
      "description": "Standard shipping in 3-5 business days",
      "totals": [{"type": "total", "amount": 599}],
    }
    problems = base.option_description_problems(repeats_title)
    self.assertTrue(any("FULFILLMENT-0021" in p for p in problems))

    repeats_total = {
      "id": "std-ship",
      "title": "Standard Shipping",
      "description": {"plain": "Arrives in 3-5 days for $5.99"},
      "totals": [{"type": "total", "amount": 599}],
    }
    problems = base.option_description_problems(repeats_total)
    self.assertTrue(any("FULFILLMENT-0021" in p for p in problems))


if __name__ == "__main__":
  unittest.main()

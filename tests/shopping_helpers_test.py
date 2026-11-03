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


if __name__ == "__main__":
  unittest.main()

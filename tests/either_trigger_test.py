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

"""Unit tests for the situations that either of two triggers can produce.

A completion stays in progress because of the item bought or because of the
payment instrument used; a total is itemized because of the item or because
of where the order ships. The fixtures declare whichever is how the server
works, and the three suite tests that need those situations take either.
They run here against the scripted store in `strict_store.py`, which comes
to both situations either way. No server or fixture file is needed.
"""

import json
import unittest
from unittest import mock

from framework.outcomes import NO_TRIGGER
from shopping import base
from shopping.checkout import lifecycle_test, totals_test
from tests.strict_store import StrictStore, failure, skipped, wired

ASYNC_TESTS = (
  "test_complete_in_progress_omits_order",
  "test_update_rejected_during_complete_in_progress",
)
LINES_TEST = "test_sub_lines_sum_equals_parent_amount"

# The test data's payment instruments: the default card, and one the store
# takes time over.
INSTRUMENTS = [
  {
    "id": "instr_1",
    "type": "card",
    "brand": "Visa",
    "last_digits": "1234",
    "token": "success_token",
    "handler_id": "mock_payment_handler",
  },
  {
    "id": "instr_auth",
    "type": "card",
    "brand": "Visa",
    "last_digits": "3155",
    "token": "needs_authentication",
    "handler_id": "mock_payment_handler",
  },
]

QUEBEC = {
  "street_address": "275 Rue Notre-Dame E",
  "address_locality": "Montreal",
  "address_region": "QC",
  "postal_code": "H2Y 1C6",
  "address_country": "CA",
}
TAX_LINES = [("GST", 250), ("QST", 499)]


def bodies(store, method, suffix=""):
  """Return the JSON bodies of the store's requests of a kind."""
  return [
    json.loads(request.content)
    for request in store.requests
    if request.method == method and request.url.path.endswith(suffix)
  ]


class RequireAnyTriggerTest(unittest.TestCase):
  """`require_any_trigger` returns the first declared trigger, or skips."""

  def _probe(self, fixtures):
    return wired(
      lifecycle_test.CheckoutLifecycleTest,
      ASYNC_TESTS[0],
      StrictStore(),
      fixtures,
    )

  def test_the_first_declared_trigger_comes_back_with_its_key(self):
    """In the order asked, whatever order the fixtures list them in."""
    both = {
      "async_completion_instrument_id": "instr_auth",
      "async_completion_item_sku": "slow_item",
    }
    keys = ("async_completion_item_sku", "async_completion_instrument_id")

    self.assertEqual(
      self._probe(both).require_any_trigger(*keys),
      ("async_completion_item_sku", "slow_item"),
    )
    self.assertEqual(
      self._probe(both).require_any_trigger(*reversed(keys)),
      ("async_completion_instrument_id", "instr_auth"),
    )
    self.assertEqual(
      self._probe(
        {"async_completion_instrument_id": "instr_auth"}
      ).require_any_trigger(*keys),
      ("async_completion_instrument_id", "instr_auth"),
    )

  def test_a_malformed_trigger_reads_as_absent(self):
    """The next one is used, as if the first were not declared."""
    probe = self._probe(
      {
        "async_completion_item_sku": "   ",
        "async_completion_instrument_id": "instr_auth",
      }
    )

    self.assertEqual(
      probe.require_any_trigger(
        "async_completion_item_sku", "async_completion_instrument_id"
      ),
      ("async_completion_instrument_id", "instr_auth"),
    )

  def test_none_declared_skips_naming_them_all(self):
    """The skip says which keys would each run the test."""
    probe = self._probe({})

    outcome = skipped(
      probe.require_any_trigger,
      "total_lines_item_sku",
      "total_lines_destination",
    )

    self.assertEqual(
      outcome, (NO_TRIGGER, "total_lines_item_sku or total_lines_destination")
    )

  def test_a_name_that_is_not_a_trigger_is_an_error(self):
    """Even when an earlier name is declared: the mistake is in the test."""
    probe = self._probe({"async_completion_item_sku": "slow_item"})

    with self.assertRaises(KeyError):
      probe.require_any_trigger("async_completion_item_sku", "valid_item")


class PaymentWithInstrumentTest(unittest.TestCase):
  """`payment_with_instrument` pays with a row of the test data, or skips."""

  def setUp(self):
    """Stand the test data in for a `payment_instruments.csv`."""
    patch = mock.patch.object(
      base.shopping_test_data, "payment_instruments", INSTRUMENTS
    )
    patch.start()
    self.addCleanup(patch.stop)
    self.probe = wired(
      lifecycle_test.CheckoutLifecycleTest, ASYNC_TESTS[0], StrictStore()
    )

  def test_the_payload_carries_the_named_instrument(self):
    """Its id and its token, as the row has them."""
    payload = self.probe.payment_with_instrument(
      "instr_auth", "async_completion_instrument_id"
    )

    instrument = payload["payment"]["instruments"][0]
    self.assertEqual(instrument["id"], "instr_auth")
    self.assertEqual(instrument["credential"]["token"], "needs_authentication")

  def test_an_id_with_no_row_skips_rather_than_pay_with_the_default(self):
    """The fixtures named an instrument the test data doesn't have."""
    outcome = skipped(
      self.probe.payment_with_instrument,
      "instr_missing",
      "async_completion_instrument_id",
    )

    self.assertEqual(
      outcome,
      (
        NO_TRIGGER,
        "async_completion_instrument_id naming a row of "
        "payment_instruments.csv (no row has the id 'instr_missing')",
      ),
    )


class AsyncCompletionSuiteTestsTest(unittest.TestCase):
  """The two `complete_in_progress` tests, by item and by instrument."""

  def setUp(self):
    """Stand the test data in for a `payment_instruments.csv`."""
    patch = mock.patch.object(
      base.shopping_test_data, "payment_instruments", INSTRUMENTS
    )
    patch.start()
    self.addCleanup(patch.stop)

  def _run(self, method, fixtures, **store_options):
    store = StrictStore(**store_options)
    test = wired(lifecycle_test.CheckoutLifecycleTest, method, store, fixtures)
    getattr(test, method)()
    return store

  def _bought_and_paid_with(self, store):
    """Return the item the checkout was created for, and the token paid."""
    created = bodies(store, "POST", "/checkout-sessions")[0]
    completed = bodies(store, "POST", "/complete")[0]
    return (
      created["line_items"][0]["item"]["id"],
      completed["payment"]["instruments"][0]["credential"]["token"],
    )

  def test_they_pass_by_instrument_on_a_store_that_goes_by_the_payment(self):
    """The default item is bought, and paid for with the declared card."""
    for method in ASYNC_TESTS:
      with self.subTest(test=method):
        store = self._run(
          method,
          {"async_completion_instrument_id": "instr_auth"},
          async_tokens={"needs_authentication"},
        )

        item, token = self._bought_and_paid_with(store)
        self.assertEqual(token, "needs_authentication")
        self.assertEqual(item, "item_1")

  def test_they_pass_by_item_as_before(self):
    """The declared item is bought, and paid for with the default card."""
    for method in ASYNC_TESTS:
      with self.subTest(test=method):
        store = self._run(
          method,
          {"async_completion_item_sku": "slow_item"},
          async_completion=True,
        )

        self.assertEqual(
          self._bought_and_paid_with(store), ("slow_item", "success_token")
        )

  def test_the_item_comes_first_when_both_are_declared(self):
    """A server that declares both is asked the way it was before."""
    store = self._run(
      ASYNC_TESTS[0],
      {
        "async_completion_item_sku": "slow_item",
        "async_completion_instrument_id": "instr_auth",
      },
      async_completion=True,
    )

    self.assertEqual(
      self._bought_and_paid_with(store), ("slow_item", "success_token")
    )

  def test_they_skip_naming_both_keys_when_neither_is_declared(self):
    """Nothing is sent to the server."""
    for method in ASYNC_TESTS:
      with self.subTest(test=method):
        store = StrictStore()
        test = wired(lifecycle_test.CheckoutLifecycleTest, method, store)

        outcome = skipped(getattr(test, method))

        self.assertEqual(
          outcome,
          (
            NO_TRIGGER,
            "async_completion_item_sku or async_completion_instrument_id",
          ),
        )
        self.assertEqual(store.requests, [])

  def test_they_skip_when_the_instrument_is_not_in_the_test_data(self):
    """Before a checkout is made for a card the suite can't present."""
    for method in ASYNC_TESTS:
      with self.subTest(test=method):
        store = StrictStore(async_tokens={"needs_authentication"})
        test = wired(
          lifecycle_test.CheckoutLifecycleTest,
          method,
          store,
          {"async_completion_instrument_id": "instr_missing"},
        )

        category, detail = skipped(getattr(test, method))

        self.assertEqual(category, NO_TRIGGER)
        self.assertIn("no row has the id 'instr_missing'", detail)
        self.assertEqual(store.requests, [])

  def test_a_store_that_completes_at_once_fails(self):
    """The fixtures said this instrument stays in progress; it didn't."""
    for method in ASYNC_TESTS:
      with self.subTest(test=method):
        message = failure(
          self._run, method, {"async_completion_instrument_id": "instr_auth"}
        )

        self.assertIn(
          "expected 'complete_in_progress', got 'completed'", message
        )


class TotalLinesSuiteTestTest(unittest.TestCase):
  """The `totals[].lines[]` test, by item and by destination."""

  def _run(self, fixtures, **store_options):
    store = StrictStore(**store_options)
    test = wired(totals_test.TotalsTest, LINES_TEST, store, fixtures)
    getattr(test, LINES_TEST)()
    return store

  def _created_for(self, store):
    """Return the item and the destination the checkout was created with."""
    created = bodies(store, "POST", "/checkout-sessions")[0]
    method = created["fulfillment"]["methods"][0]
    destination = next(
      d
      for d in method["destinations"]
      if d["id"] == method["selected_destination_id"]
    )
    return created["line_items"][0]["item"]["id"], destination

  def test_it_passes_by_destination_on_a_store_that_goes_by_the_address(self):
    """The checkout ships to the declared address, sent as declared."""
    store = self._run(
      {"total_lines_destination": QUEBEC},
      tax_lines_by_country={"CA": TAX_LINES},
    )

    item, destination = self._created_for(store)
    self.assertEqual(item, "item_1")
    self.assertEqual(
      destination, {**QUEBEC, "id": "dest_1", "type": "shipping_address"}
    )
    checkout = next(iter(store.checkouts.values()))
    method = checkout["fulfillment"]["methods"][0]
    self.assertEqual(method["selected_destination_id"], "dest_1")
    self.assertTrue(method["groups"][0]["selected_option_id"])
    tax = next(t for t in checkout["totals"] if t["type"] == "tax")
    self.assertEqual(tax["amount"], 749)
    self.assertEqual(len(tax["lines"]), 2)

  def test_only_the_declared_fields_of_the_destination_are_sent(self):
    """Nothing is defaulted: the address decides what the business does."""
    store = self._run(
      {"total_lines_destination": {"address_country": "CA"}},
      tax_lines_by_country={"CA": TAX_LINES},
    )

    self.assertEqual(
      self._created_for(store)[1],
      {"address_country": "CA", "id": "dest_1", "type": "shipping_address"},
    )

  def test_sub_lines_that_do_not_add_up_fail(self):
    """The rule is checked on the breakdown the destination brought."""
    message = failure(
      self._run,
      {"total_lines_destination": QUEBEC},
      tax_lines_by_country={"CA": TAX_LINES},
      tax_lines_off_by=1,
    )

    self.assertIn(
      "sum(lines[].amount) == 749 does not equal parent amount 750", message
    )

  def test_a_declared_destination_without_a_breakdown_fails(self):
    """The fixtures said this address itemizes a total; it didn't."""
    message = failure(self._run, {"total_lines_destination": QUEBEC}, tax=700)

    self.assertIn(
      "expected at least one totals[] entry with a lines[] breakdown", message
    )

  def test_it_passes_by_item_as_before(self):
    """The declared item is bought and ships to the default address."""
    store = self._run(
      {"total_lines_item_sku": "split_tax"},
      tax_lines_by_item={"split_tax": TAX_LINES},
    )

    item, destination = self._created_for(store)
    self.assertEqual(item, "split_tax")
    self.assertEqual(destination["address_country"], "US")

  def test_the_item_comes_first_when_both_are_declared(self):
    """A server that declares both is asked the way it was before."""
    store = self._run(
      {"total_lines_item_sku": "split_tax", "total_lines_destination": QUEBEC},
      tax_lines_by_item={"split_tax": TAX_LINES},
      tax_lines_by_country={"CA": [("GST", 1), ("QST", 2)]},
    )

    item, destination = self._created_for(store)
    self.assertEqual(item, "split_tax")
    self.assertEqual(destination["address_country"], "US")

  def test_a_default_checkout_with_a_breakdown_needs_no_trigger(self):
    """A store that itemizes every checkout's tax is checked as it is."""
    store = self._run({}, tax_lines_by_country={"US": TAX_LINES})

    self.assertEqual(self._created_for(store)[1]["address_country"], "US")

  def test_it_skips_naming_both_keys_without_a_breakdown_or_a_trigger(self):
    """A default checkout with no `lines[]` gives the rule nothing to check."""
    store = StrictStore(tax=700)
    test = wired(totals_test.TotalsTest, LINES_TEST, store)

    outcome = skipped(getattr(test, LINES_TEST))

    self.assertEqual(
      outcome, (NO_TRIGGER, "total_lines_item_sku or total_lines_destination")
    )


if __name__ == "__main__":
  unittest.main()

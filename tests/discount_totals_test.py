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

"""Unit tests for the discount tests and the totals they read a discount in.

discount.md reports a discount allocated to line items as an
`items_discount` entry of `totals[]`, and any other as `discount`. Four
suite tests looked for `discount` alone. They run here against the scripted
store in `strict_store.py`, which reports a code's discount under either
type, discounts a checkout on its own account when told to, and gets
discounts wrong in the ways the four tests are there to catch. No server or
fixture file is needed.
"""

import unittest

from shopping import base
from shopping.discount import discount_test, semantics_test
from tests.strict_store import StrictStore, failure, wired

CODE = "Save5"
# The four suite tests. The classes are reached through their modules, so
# that pytest does not collect them from here.
CASE_TEST = (
  semantics_test.DiscountSemanticsTest,
  "test_code_matches_case_insensitively",
)
NEGATIVE_TEST = (
  semantics_test.DiscountSemanticsTest,
  "test_discount_total_is_negative",
)
CLEARING_TEST = (
  semantics_test.DiscountSemanticsTest,
  "test_empty_codes_removes_discount",
)
UNKNOWN_CODE_TEST = (discount_test.DiscountTest, "test_unknown_discount_code")
DISCOUNT_TESTS = (CASE_TEST, NEGATIVE_TEST, CLEARING_TEST, UNKNOWN_CODE_TEST)
TOTAL_TYPES = ("items_discount", "discount")


def run(suite_test, **store_options):
  """Run one of the four suite tests against a store that takes `CODE`."""
  case, method = suite_test
  store = StrictStore(**{"discount_codes": {CODE: 500}, **store_options})
  test = wired(case, method, store, {"valid_discount_code": CODE})
  # What DiscountSemanticsTest.setUp would read from the fixtures.
  test._code = test.fixture_ctx.get_test_discount_code()
  getattr(test, method)()
  return store


class DiscountTotalEntriesTest(unittest.TestCase):
  """The helpers that find a discount in `totals[]`."""

  TOTALS = [
    {"type": "subtotal", "amount": 5000},
    {"type": "items_discount", "amount": -500},
    {"type": "fulfillment", "amount": 599},
    {"type": "discount", "amount": -100},
    {"type": "fee", "amount": 50},
    "not an entry",
    {"type": "total", "amount": 5049},
  ]

  def test_entries_of_both_types_are_found_in_their_order(self):
    """`items_discount` and `discount`; no other type, and no stray value."""
    self.assertEqual(
      base.discount_total_entries(self.TOTALS),
      [
        {"type": "items_discount", "amount": -500},
        {"type": "discount", "amount": -100},
      ],
    )

  def test_totals_that_are_not_a_list_have_no_entries(self):
    """A missing or malformed `totals` reads as no discount."""
    for totals in (None, {}, "totals", 7):
      with self.subTest(totals=totals):
        self.assertEqual(base.discount_total_entries(totals), [])
        self.assertEqual(base.discount_amounts(totals), [])

  def test_amounts_compare_whatever_the_order_of_the_entries(self):
    """The pairs are sorted, so two responses compare by what they carry."""
    self.assertEqual(
      base.discount_amounts(self.TOTALS),
      [("discount", -100), ("items_discount", -500)],
    )
    self.assertEqual(
      base.discount_amounts(self.TOTALS),
      base.discount_amounts(list(reversed(self.TOTALS))),
    )

  def test_an_entry_without_an_amount_still_compares(self):
    """A malformed entry is the caller's to judge; it must not raise here."""
    self.assertEqual(
      base.discount_amounts(
        [{"type": "discount"}, {"type": "discount", "amount": -100}]
      ),
      [("discount", -100), ("discount", None)],
    )

  def test_the_two_types_are_the_ones_the_schema_makes_negative(self):
    """The totals tests check the sign of the same two types."""
    self.assertEqual(
      base.DISCOUNT_TOTAL_TYPES, frozenset({"discount", "items_discount"})
    )
    self.assertLessEqual(base.DISCOUNT_TOTAL_TYPES, base.WELL_KNOWN_TOTAL_TYPES)


class DiscountSuiteTestsTest(unittest.TestCase):
  """The four suite tests, on stores that report a discount either way."""

  def test_they_pass_whichever_type_the_discount_is_reported_under(self):
    """A store that allocates discounts to line items is not failed for it."""
    for total in TOTAL_TYPES:
      for suite_test in DISCOUNT_TESTS:
        with self.subTest(test=suite_test[1], total=total):
          store = run(suite_test, discount_total=total)

          self.assertTrue(store.requests)

  def test_the_store_reports_the_discount_under_the_type_asked_for(self):
    """What the passes above rest on: the totals carry that type alone."""
    for total in TOTAL_TYPES:
      with self.subTest(total=total):
        store = StrictStore(discount_codes={CODE: 500}, discount_total=total)
        probe = wired(*NEGATIVE_TEST, store)
        created = probe.create_checkout_session(select_fulfillment=False)

        discounted = probe.update_checkout_session(
          semantics_test.checkout.Checkout(**created),
          discounts={"codes": [CODE.upper()]},
        )

        self.assertEqual(base.discount_amounts(created["totals"]), [])
        self.assertEqual(
          base.discount_amounts(discounted["totals"]), [(total, -500)]
        )

  def test_a_discount_of_the_stores_own_fails_nothing(self):
    """What a code did is read against what the checkout carried before."""
    for total in TOTAL_TYPES:
      for suite_test in DISCOUNT_TESTS:
        with self.subTest(test=suite_test[1], total=total):
          run(suite_test, discount_total=total, automatic_discount=300)

  def test_a_discount_that_outlives_its_code_fails(self):
    """Clearing the codes must take the code's discount off the totals."""
    for total in TOTAL_TYPES:
      with self.subTest(total=total):
        message = failure(
          run, CLEARING_TEST, discount_total=total, discount_fault="sticky"
        )

        self.assertIn("codes:[] must remove the discount", message)

  def test_a_discount_for_an_unknown_code_fails(self):
    """Under either type: `items_discount` used to pass unnoticed."""
    for total in TOTAL_TYPES:
      with self.subTest(total=total):
        message = failure(
          run,
          UNKNOWN_CODE_TEST,
          discount_total=total,
          discount_fault="any_code",
        )

        self.assertIn(
          "Unknown discount code should not apply discount", message
        )

  def test_a_positive_discount_total_fails(self):
    """The sign is checked on the entry of either type."""
    for total in TOTAL_TYPES:
      with self.subTest(total=total):
        message = failure(
          run, NEGATIVE_TEST, discount_total=total, discount_fault="positive"
        )

        self.assertIn(
          f"a {total} totals[] entry must be negative, got 500", message
        )

  def test_a_positive_total_beside_a_negative_one_fails(self):
    """Every entry is checked, not the first one found.

    The store's own discount is an `items_discount` of -300, as it should
    be; the code's, a `discount`, comes after it with the wrong sign.
    """
    message = failure(
      run,
      NEGATIVE_TEST,
      discount_total="discount",
      automatic_discount=300,
      discount_fault="positive",
    )

    self.assertIn(
      "a discount totals[] entry must be negative, got 500", message
    )

  def test_a_valid_code_that_adds_no_discount_total_fails(self):
    """A store that doesn't know the code prices nothing for it."""
    for suite_test, complaint in (
      (NEGATIVE_TEST, "applying a valid code must add a discount total"),
      (CLEARING_TEST, "precondition: code applied"),
    ):
      with self.subTest(test=suite_test[1]):
        message = failure(run, suite_test, discount_codes={})

        self.assertIn(complaint, message)

  def test_an_applied_code_that_takes_nothing_off_fails(self):
    """Listing the code in discounts.applied is not a discount total."""
    for suite_test, complaint in (
      (CASE_TEST, "a matched discount must produce a discount total"),
      (NEGATIVE_TEST, "applying a valid code must add a discount total"),
    ):
      with self.subTest(test=suite_test[1]):
        message = failure(run, suite_test, discount_fault="unpriced")

        self.assertIn(complaint, message)

  def test_a_code_matched_by_case_alone_fails(self):
    """The case test still fails a store that matches case-sensitively."""
    message = failure(run, CASE_TEST, discount_fault="case_sensitive")

    self.assertIn("should match and appear in discounts.applied", message)


if __name__ == "__main__":
  unittest.main()

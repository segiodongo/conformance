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

"""Unit tests for the suite tests that edit an order with `PUT /orders/{id}`.

The spec defines only Get Order; `PUT /orders/{id}` is the sample server's
own endpoint. Four suite tests use it. They run here against the scripted
store in `strict_store.py`, which serves Get Order and answers the PUT with
405 unless told otherwise, the way a business that implements the spec and
nothing more does. No server or fixture file is needed.
"""

from pathlib import Path
import unittest

from framework.outcomes import NO_TRIGGER, skip_category
from shopping import base
from shopping.order import order_test
from shopping.validation import invalid_input_test
from tests import strict_store
from tests.strict_store import wired

# The address `test_order_update` sends comes from the sample's CSV data.
FLOWER_SHOP = Path(base.__file__).resolve().parent / "fixtures" / "flower_shop"

# The suite tests that send `PUT /orders/{id}`. The classes are reached
# through their modules, so that pytest does not collect them from here.
PUT_TESTS = (
  (order_test.OrderTest, "test_order_update"),
  (order_test.OrderTest, "test_order_adjustments"),
  (invalid_input_test.InvalidInputTest, "test_invalid_adjustment_status"),
  (invalid_input_test.InvalidInputTest, "test_malformed_adjustment_payload"),
)


def order_requests(store: strict_store.StrictStore) -> list[str]:
  """Return the methods of the requests the store got on an order."""
  return [r.method for r in store.requests if "/orders/" in r.url.path]


class PutOrderTest(unittest.TestCase):
  """`put_order` skips without the endpoint and returns any other answer."""

  def _probe(self, **store_options):
    store = strict_store.StrictStore(**store_options)
    probe = wired(order_test.OrderTest, "test_order_adjustments", store)
    return probe, probe.create_completed_order()

  def test_404_and_405_skip_as_no_trigger(self):
    """The skip names the endpoint, the answer and what the spec defines."""
    for status in (404, 405):
      with self.subTest(status=status):
        probe, order_id = self._probe(order_put=status)

        with self.assertRaises(unittest.SkipTest) as skipped:
          probe.put_order(order_id, {"id": order_id})

        category, detail = skip_category(str(skipped.exception))
        self.assertEqual(category, NO_TRIGGER)
        self.assertEqual(
          detail,
          "a PUT /orders/{id} endpoint that edits an order (the server "
          f"answered {status}); the spec defines only GET /orders/{{id}}",
        )

  def test_any_other_answer_is_returned(self):
    """A 200, a 422 or a 401 is the test's to judge, not a skip."""
    for status in (200, 422, 401):
      with self.subTest(status=status):
        probe, order_id = self._probe(order_put=status)

        response = probe.put_order(order_id, {"id": order_id})

        self.assertEqual(response.status_code, status)

  def test_the_body_and_the_headers_are_sent(self):
    """The request carries the JSON body and the suite's own headers."""
    store = strict_store.StrictStore(order_put=200)
    probe = wired(order_test.OrderTest, "test_order_adjustments", store)
    order_id = probe.create_completed_order()

    probe.put_order(order_id, {"id": order_id, "note": "x"})

    sent = store.requests[-1]
    self.assertEqual(sent.method, "PUT")
    self.assertTrue(sent.url.path.endswith(f"/orders/{order_id}"))
    self.assertEqual(store.orders[order_id], {"id": order_id, "note": "x"})
    self.assertIn("ucp-agent", sent.headers)


class OrderPutSuiteTestsTest(unittest.TestCase):
  """The four suite tests, against stores with and without the endpoint."""

  def setUp(self):
    """Load the CSV data `test_order_update` takes its address from."""
    base.shopping_test_data.load(FLOWER_SHOP)

  def test_they_skip_on_a_store_without_the_endpoint(self):
    """Each reads the order, sends the PUT, and skips on a 405 or a 404."""
    for status in (405, 404):
      for case, method in PUT_TESTS:
        with self.subTest(test=method, status=status):
          store = strict_store.StrictStore(order_put=status)
          test = wired(case, method, store)

          with self.assertRaises(unittest.SkipTest) as skipped:
            getattr(test, method)()

          category, detail = skip_category(str(skipped.exception))
          self.assertEqual(category, NO_TRIGGER)
          self.assertIn("PUT /orders/{id}", detail)
          self.assertIn(f"answered {status}", detail)
          # Get Order came first and succeeded; the PUT is what was refused.
          self.assertEqual(order_requests(store), ["GET", "PUT"])

  def test_adjustments_pass_on_a_store_with_the_endpoint(self):
    """A store that keeps what is PUT passes `test_order_adjustments`."""
    store = strict_store.StrictStore(order_put=200)
    test = wired(order_test.OrderTest, "test_order_adjustments", store)

    test.test_order_adjustments()

    kept = next(iter(store.orders.values()))
    self.assertEqual(kept["adjustments"][0]["type"], "refund")

  def test_a_wrong_answer_from_the_endpoint_still_fails(self):
    """An endpoint that accepts an invalid adjustment fails, not skips."""
    store = strict_store.StrictStore(order_put=200)
    test = wired(
      invalid_input_test.InvalidInputTest,
      "test_invalid_adjustment_status",
      store,
    )

    with self.assertRaises(AssertionError) as failed:
      test.test_invalid_adjustment_status()

    self.assertIn("Expected status [422], got 200", str(failed.exception))

  def test_get_order_is_checked_before_its_body_is_read(self):
    """A refused Get Order fails on its status, and no PUT follows.

    Before, both tests handed the refusal's body to the order model and
    stopped on its validation error.
    """
    for method in (
      "test_invalid_adjustment_status",
      "test_malformed_adjustment_payload",
    ):
      with self.subTest(test=method):
        store = strict_store.StrictStore(order_get=401)
        test = wired(invalid_input_test.InvalidInputTest, method, store)

        with self.assertRaises(AssertionError) as failed:
          getattr(test, method)()

        self.assertIn(
          "Expected status [200], got 401 for GET", str(failed.exception)
        )
        self.assertEqual(order_requests(store), ["GET"])


if __name__ == "__main__":
  unittest.main()

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

"""Unit tests for the request that updates an order, and the tests that use it.

The specification defines no operation that updates an order, so the suite
sends the request the fixtures declare (`order_update_request`) and watches
for the order to change. The helpers and the five suite tests that use them
run here against the scripted store in `strict_store.py`, whose admin route
updates an order in more ways than the sample server's hook does, and whose
deliveries stand in for a webhook receiver. No server, socket or fixture
file is needed.
"""

import json
import types
import unittest

from absl.testing import flagsaver
from framework.outcomes import ENVIRONMENT, NO_TRIGGER
from shopping import base
from shopping.order import (
  simulation_url_security_test,
  webhook_structure_test,
  webhook_test,
)
from tests.strict_store import (
  ORDER_UPDATE_REQUEST,
  StrictStore,
  failure,
  skipped,
  wired,
)

SECRET = "admin-secret"

# The suite tests that need an order updated. The classes are reached
# through their modules, so that pytest does not collect them from here.
UPDATE_TESTS = (
  (webhook_test.WebhookTest, "test_webhook_event_stream"),
  (
    webhook_structure_test.WebhookStructureTest,
    "test_update_event_is_full_order_entity",
  ),
)
SECRET_TESTS = (
  "test_simulation_endpoint_missing_header",
  "test_simulation_endpoint_incorrect_secret",
  "test_simulation_endpoint_correct_secret",
)


def suite_test(case, method, store, declared=ORDER_UPDATE_REQUEST):
  """Return a suite test wired to a store, with its deliveries to read.

  Args:
    case: The suite's test class.
    method: The name of the test method.
    store: The scripted store.
    declared: The `order_update_request` the fixtures declare, or None for
      fixtures without one.

  """
  fixtures = {} if declared is None else {"order_update_request": declared}
  test = wired(case, method, store, fixtures)
  # What the test's setUp would start: a receiver. The store's own list of
  # deliveries has the shape of the receiver's events.
  test.webhook_server = types.SimpleNamespace(events=store.deliveries)
  return test


def admin_requests(store):
  """Return the requests the store got on its admin route."""
  return [r for r in store.requests if r.url.path.startswith("/admin/")]


class FillOrderUpdateRequestTest(unittest.TestCase):
  """`fill_order_update_request` fills a declared request in for an order."""

  DECLARED = {
    "method": "PATCH",
    "url": "/admin/orders/{order_id}?key={simulation_secret}",
    "headers": {
      "X-Admin-Secret": "{simulation_secret}",
      "X-Order": "order {order_id}",
      "X-Static": "as-is",
    },
    "body": {
      "order": "{order_id}",
      "notes": ["ship {order_id}", {"token": "{simulation_secret}"}],
      "count": 1,
      "{order_id}": "a key is left alone",
      "template": '{"json": "{left alone}"}',
    },
  }

  def test_the_placeholders_are_filled_everywhere(self):
    """The URL, the header values and the strings of the body."""
    filled = base.fill_order_update_request(
      self.DECLARED, "ord_7", "the-secret"
    )

    self.assertEqual(
      filled,
      {
        "method": "PATCH",
        "url": "/admin/orders/ord_7?key=the-secret",
        "headers": {
          "X-Admin-Secret": "the-secret",
          "X-Order": "order ord_7",
          "X-Static": "as-is",
        },
        "body": {
          "order": "ord_7",
          "notes": ["ship ord_7", {"token": "the-secret"}],
          "count": 1,
          "{order_id}": "a key is left alone",
          "template": '{"json": "{left alone}"}',
        },
      },
    )
    # The declared request is the fixtures' own, and is not written to.
    self.assertEqual(self.DECLARED["body"]["order"], "{order_id}")

  def test_method_and_headers_have_defaults_and_a_body_stays_out(self):
    """Only a URL is needed: POST, no headers, and no body is sent."""
    filled = base.fill_order_update_request(
      {"url": "/ship/{order_id}"}, "o", ""
    )

    self.assertEqual(
      filled, {"method": "POST", "url": "/ship/o", "headers": {}}
    )

  def test_without_its_secret(self):
    """A header that carries the secret goes; elsewhere it becomes nothing."""
    filled = base.fill_order_update_request(self.DECLARED, "ord_7", None)

    self.assertEqual(
      filled["headers"], {"X-Order": "order ord_7", "X-Static": "as-is"}
    )
    self.assertEqual(filled["url"], "/admin/orders/ord_7?key=")
    self.assertEqual(filled["body"]["notes"][1], {"token": ""})

  def test_uses_simulation_secret(self):
    """The placeholder counts in the URL, a header value or the body."""
    for declared in (
      {"url": "/ship/{order_id}?key={simulation_secret}"},
      {"url": "/ship", "headers": {"X-Secret": "{simulation_secret}"}},
      {"url": "/ship", "body": {"auth": ["Bearer {simulation_secret}"]}},
    ):
      with self.subTest(declared=declared):
        self.assertTrue(base.uses_simulation_secret(declared))
    for declared in (
      {"url": "/ship/{order_id}"},
      {"url": "/ship", "headers": {"Authorization": "Bearer static"}},
      {"url": "/ship", "body": {"{simulation_secret}": "a key"}},
    ):
      with self.subTest(declared=declared):
        self.assertFalse(base.uses_simulation_secret(declared))

  def test_the_description_keeps_the_placeholders(self):
    """A message that quotes the request shows no order and no secret."""
    self.assertEqual(
      base.describe_order_update_request(self.DECLARED),
      "PATCH /admin/orders/{order_id}?key={simulation_secret}",
    )
    self.assertEqual(
      base.describe_order_update_request({"url": "/ship/{order_id}"}),
      "POST /ship/{order_id}",
    )


class RequestOrderUpdateTest(unittest.TestCase):
  """`request_order_update` sends the request, or skips where it can't."""

  def _probe(self, declared=ORDER_UPDATE_REQUEST, **store_options):
    store = StrictStore(**{"order_update": "ship", **store_options})
    probe = suite_test(
      webhook_test.WebhookTest, "test_webhook_event_stream", store, declared
    )
    return probe, store, probe.create_completed_order()

  def test_the_declared_request_is_sent_with_the_suites_headers(self):
    """The order and the secret are filled in, and the order is updated."""
    probe, store, order_id = self._probe()

    with flagsaver.flagsaver(simulation_secret=SECRET):
      response = probe.request_order_update(order_id)

    self.assertEqual(response.status_code, 200)
    sent = admin_requests(store)[-1]
    self.assertEqual(sent.method, "POST")
    self.assertEqual(sent.url.path, f"/admin/orders/{order_id}/update")
    self.assertEqual(sent.headers["x-admin-secret"], SECRET)
    self.assertIn("ucp-agent", sent.headers)
    self.assertEqual(sent.content, b"")
    self.assertTrue(store.orders[order_id]["fulfillment"]["events"])

  def test_a_full_url_a_method_and_a_body_are_sent_as_declared(self):
    """A declared header replaces the suite's of that name, in any case."""
    probe, store, order_id = self._probe(
      declared={
        "method": "post",
        "url": "https://store.example/admin/orders/{order_id}/update",
        "headers": {
          "X-Admin-Secret": "{simulation_secret}",
          "REQUEST-ID": "update-{order_id}",
        },
        "body": {"order": "{order_id}", "carrier": "post"},
      }
    )

    with flagsaver.flagsaver(simulation_secret=SECRET):
      probe.request_order_update(order_id)

    sent = admin_requests(store)[-1]
    self.assertEqual(sent.method, "POST")
    self.assertEqual(
      json.loads(sent.content), {"order": order_id, "carrier": "post"}
    )
    self.assertEqual(
      sent.headers.get_list("request-id"), [f"update-{order_id}"]
    )

  def test_no_request_declared_skips_naming_the_key(self):
    """Nothing is sent for fixtures without the key."""
    probe, store, order_id = self._probe(declared=None)

    outcome = skipped(probe.request_order_update, order_id)

    self.assertEqual(outcome, (NO_TRIGGER, "order_update_request"))
    self.assertEqual(admin_requests(store), [])

  def test_a_server_without_the_endpoint_skips_as_no_trigger(self):
    """A 404 or a 405 means the declared request is another server's."""
    for status, options, declared in (
      (404, {"order_update": None}, ORDER_UPDATE_REQUEST),
      (405, {}, {**ORDER_UPDATE_REQUEST, "method": "PUT"}),
    ):
      with self.subTest(status=status):
        probe, _, order_id = self._probe(declared, **options)

        with flagsaver.flagsaver(simulation_secret=SECRET):
          outcome = skipped(probe.request_order_update, order_id)

        self.assertEqual(
          outcome,
          (
            NO_TRIGGER,
            "an order_update_request the server has an endpoint for (it "
            f"answered {declared.get('method', 'POST')} "
            f"/admin/orders/{{order_id}}/update with {status})",
          ),
        )

  def test_a_refused_secret_is_the_environment(self):
    """A 401 or a 403 says how to lift it, and never shows the secret."""
    for status in (403, 401):
      with self.subTest(status=status):
        probe, _, order_id = self._probe(update_refusal=status)

        with flagsaver.flagsaver(simulation_secret="not-the-secret"):
          outcome = skipped(probe.request_order_update, order_id)

        self.assertEqual(
          outcome,
          (
            ENVIRONMENT,
            "the simulation secret was rejected (POST "
            f"/admin/orders/{{order_id}}/update -> {status}); pass "
            "--simulation_secret matching the server under test",
          ),
        )

  def test_a_refused_request_without_a_secret_names_the_fixtures(self):
    """The credential is then one the fixtures declare themselves."""
    probe, _, order_id = self._probe(
      declared={
        "url": "/admin/orders/{order_id}/update",
        "headers": {"X-Admin-Secret": "written-in-the-fixtures"},
      }
    )

    outcome = skipped(probe.request_order_update, order_id)

    self.assertEqual(
      outcome,
      (
        ENVIRONMENT,
        "the server refused the order_update_request (POST "
        "/admin/orders/{order_id}/update -> 403); check the credentials "
        "its headers declare in test_fixtures",
      ),
    )

  def test_any_other_answer_outside_2xx_fails(self):
    """A hook that breaks is not a skip; the message keeps the secret out."""
    probe, _, order_id = self._probe(update_refusal=500)

    with flagsaver.flagsaver(simulation_secret="not-the-secret"):
      message = failure(probe.request_order_update, order_id)

    self.assertIn(
      "The order_update_request failed: POST /admin/orders/{order_id}/update "
      "answered 500, not a 2xx",
      message,
    )
    self.assertNotIn("not-the-secret", message)

  def test_another_secret_or_none_can_be_sent(self):
    """`send_order_update_request` returns whatever the server answers."""
    probe, store, order_id = self._probe()

    wrong = probe.send_order_update_request(order_id, "wrong")
    self.assertEqual(wrong.status_code, 403)
    self.assertEqual(
      admin_requests(store)[-1].headers["x-admin-secret"], "wrong"
    )

    missing = probe.send_order_update_request(order_id, None)
    self.assertEqual(missing.status_code, 403)
    self.assertNotIn("x-admin-secret", admin_requests(store)[-1].headers)


class TriggerOrderUpdateTest(unittest.TestCase):
  """`trigger_order_update` waits for a delivery that shows the update."""

  def _update(self, **store_options):
    """Update an order on a store; return the store and what came back."""
    store = StrictStore(**store_options)
    probe = suite_test(
      webhook_test.WebhookTest, "test_webhook_event_stream", store
    )
    order_id = probe.create_completed_order()
    with flagsaver.flagsaver(simulation_secret=SECRET):
      return store, probe.trigger_order_update(
        order_id, lambda: list(store.deliveries)
      )

  def test_a_shipment_comes_back_as_one_update(self):
    """The order as it was, and the delivery that carries the shipment."""
    _, (before, updates) = self._update(order_update="ship")

    self.assertEqual(before["fulfillment"]["events"], [])
    self.assertEqual(len(updates), 1)
    self.assertEqual(
      [e["type"] for e in updates[0]["payload"]["fulfillment"]["events"]],
      ["shipped"],
    )

  def test_the_update_need_not_be_a_shipment(self):
    """A refund changes the order too, and that is all that is asked."""
    _, (before, updates) = self._update(order_update="refund")

    self.assertNotIn("adjustments", before)
    self.assertEqual(updates[0]["payload"]["adjustments"][0]["type"], "refund")
    self.assertEqual(updates[0]["payload"]["fulfillment"]["events"], [])

  def test_a_request_that_changes_nothing_skips(self):
    """The trigger is for a request that updates the order."""
    self.assertEqual(
      skipped(self._update, order_update="nothing"),
      (
        NO_TRIGGER,
        "an order_update_request that changes the order (POST "
        "/admin/orders/{order_id}/update answered 200, and afterwards Get "
        "Order returned the order as it was and no delivery showed a "
        "change)",
      ),
    )

  def test_a_changed_order_with_no_delivery_fails(self):
    """Get Order shows the update; the business sent nothing about it."""
    message = failure(self._update, order_update="ship", update_delivery="none")

    self.assertIn("No order-event webhook showed the order's update", message)
    self.assertIn("MUST send the full order entity on updates", message)
    self.assertIn("$.fulfillment: extra", message)
    self.assertIn("the order as it was has none", message)
    self.assertIn("No delivery followed.", message)

  def test_a_delivery_of_the_order_as_it_was_fails(self):
    """A delivery after the update that doesn't show it is not the update."""
    message = failure(
      self._update, order_update="ship", update_delivery="stale"
    )

    self.assertIn(
      "The deliveries that followed (1) carry the order as it was.", message
    )

  def test_a_delta_counts_as_a_delivery_that_shows_a_change(self):
    """Whether it is the full entity is for the test to judge."""
    _, (_, updates) = self._update(order_update="ship", update_delivery="delta")

    self.assertEqual(sorted(updates[0]["payload"]), ["fulfillment", "id"])

  def test_it_waits_for_a_delivery_that_arrives_later(self):
    """The receiver is read again until the update's delivery is there."""
    store = StrictStore(order_update="ship", update_delivery="none")
    probe = suite_test(
      webhook_test.WebhookTest, "test_webhook_event_stream", store
    )
    order_id = probe.create_completed_order()
    reads = []

    def deliveries():
      reads.append(len(store.deliveries))
      if len(reads) == 5:
        # The business gets round to it on the fifth look.
        store._deliver(store.orders[order_id])
      return list(store.deliveries)

    with flagsaver.flagsaver(simulation_secret=SECRET):
      _, updates = probe.trigger_order_update(order_id, deliveries)

    self.assertEqual(len(updates), 1)
    self.assertGreaterEqual(len(reads), 5)


class OrderUpdateSuiteTestsTest(unittest.TestCase):
  """The two suite tests that need an order updated, on stores of all kinds."""

  def _run(self, case, method, declared=ORDER_UPDATE_REQUEST, **store_options):
    store = StrictStore(**store_options)
    test = suite_test(case, method, store, declared)
    with flagsaver.flagsaver(simulation_secret=SECRET):
      getattr(test, method)()
    return store

  def test_they_pass_on_a_store_that_ships_or_refunds(self):
    """Any update will do, as long as its delivery is the full entity."""
    for update in ("ship", "refund"):
      for case, method in UPDATE_TESTS:
        with self.subTest(test=method, update=update):
          store = self._run(case, method, order_update=update)

          self.assertEqual(len(store.deliveries), 2)
          self.assertEqual(len(admin_requests(store)), 1)

  def test_they_skip_without_the_request_after_the_order_is_placed(self):
    """What comes before the update is still checked, then the test skips."""
    for case, method in UPDATE_TESTS:
      with self.subTest(test=method):
        store = StrictStore(order_update="ship")
        test = suite_test(case, method, store, None)

        outcome = skipped(getattr(test, method))

        self.assertEqual(outcome, (NO_TRIGGER, "order_update_request"))
        self.assertEqual(len(store.deliveries), 1)
        self.assertEqual(admin_requests(store), [])

  def test_they_skip_on_a_store_without_the_endpoint(self):
    """The flower shop's request means nothing to another server."""
    for case, method in UPDATE_TESTS:
      with self.subTest(test=method):
        test = suite_test(case, method, StrictStore())

        with flagsaver.flagsaver(simulation_secret=SECRET):
          category, detail = skipped(getattr(test, method))

        self.assertEqual(category, NO_TRIGGER)
        self.assertIn("the server has an endpoint for", detail)
        self.assertIn("with 404", detail)

  def test_they_skip_as_the_environment_on_a_wrong_secret(self):
    """The run's --simulation_secret is not the server's."""
    for case, method in UPDATE_TESTS:
      with self.subTest(test=method):
        test = suite_test(case, method, StrictStore(order_update="ship"))

        with flagsaver.flagsaver(simulation_secret="not-the-secret"):
          category, detail = skipped(getattr(test, method))

        self.assertEqual(category, ENVIRONMENT)
        self.assertIn("pass --simulation_secret", detail)

  def test_they_skip_when_the_request_changes_nothing(self):
    """No update happened, so there is no update delivery to judge."""
    for case, method in UPDATE_TESTS:
      with self.subTest(test=method):
        test = suite_test(case, method, StrictStore(order_update="nothing"))

        with flagsaver.flagsaver(simulation_secret=SECRET):
          category, detail = skipped(getattr(test, method))

        self.assertEqual(category, NO_TRIGGER)
        self.assertIn("an order_update_request that changes the order", detail)

  def test_they_fail_when_no_delivery_shows_the_update(self):
    """Nothing delivered, or the order as it was: both fail both tests."""
    for delivery in ("none", "stale"):
      for case, method in UPDATE_TESTS:
        with self.subTest(test=method, delivery=delivery):
          message = failure(
            self._run,
            case,
            method,
            order_update="ship",
            update_delivery=delivery,
          )

          self.assertIn(
            "No order-event webhook showed the order's update", message
          )

  def test_a_delta_fails_them_for_what_it_leaves_out(self):
    """The id and the changed member alone are not the order entity."""
    for (case, method), complaint in zip(
      UPDATE_TESTS,
      (
        # The stream test checks each delivery names the checkout.
        "None != 'chk_1'",
        "the update delivery is not a fully populated order entity",
      ),
      strict=True,
    ):
      with self.subTest(test=method):
        message = failure(
          self._run,
          case,
          method,
          order_update="ship",
          update_delivery="delta",
        )

        self.assertIn(complaint, message)


class SecretTestsTest(unittest.TestCase):
  """The three tests of the secret the declared request takes."""

  def _test(self, method, declared=ORDER_UPDATE_REQUEST, **store_options):
    store = StrictStore(**{"order_update": "ship", **store_options})
    test = suite_test(
      simulation_url_security_test.SecurityTest, method, store, declared
    )
    return test, store

  def test_they_pass_on_a_store_that_refuses_with_403_or_401(self):
    """Without the secret and with a wrong one; the right one is taken."""
    for status in (403, 401):
      for method in SECRET_TESTS:
        with self.subTest(test=method, status=status):
          test, store = self._test(method, update_refusal=status)

          with flagsaver.flagsaver(simulation_secret=SECRET):
            getattr(test, method)()

          sent = admin_requests(store)[-1]
          self.assertEqual(
            sent.headers.get("x-admin-secret"),
            {
              "test_simulation_endpoint_missing_header": None,
              "test_simulation_endpoint_incorrect_secret": (
                "for-sure-incorrect-secret"
              ),
              "test_simulation_endpoint_correct_secret": SECRET,
            }[method],
          )

  def test_a_store_that_hides_the_route_from_other_callers_passes(self):
    """A 404 without the secret, and the request as declared is answered."""
    for method in SECRET_TESTS:
      with self.subTest(test=method):
        test, store = self._test(method, update_refusal=404)

        with flagsaver.flagsaver(simulation_secret=SECRET):
          getattr(test, method)()

        self.assertEqual(
          admin_requests(store)[-1].headers["x-admin-secret"], SECRET
        )

  def test_a_store_that_takes_any_caller_fails_the_two_refusal_tests(self):
    """The request went through without the right secret."""
    for method in SECRET_TESTS[:2]:
      with self.subTest(test=method):
        test, _ = self._test(method, update_secret=None)

        with flagsaver.flagsaver(simulation_secret=SECRET):
          message = failure(getattr(test, method))

        self.assertIn(
          "POST /admin/orders/{order_id}/update was not refused without "
          "the right secret: the server answered 200, not 401 or 403",
          message,
        )

  def test_they_skip_without_a_request_or_without_a_secret_in_it(self):
    """Nothing is sent: there is no secret to leave out or get wrong."""
    static = {
      "url": "/admin/orders/{order_id}/update",
      "headers": {"X-Admin-Secret": SECRET},
    }
    for declared, needed in (
      (None, "order_update_request"),
      (
        static,
        "an order_update_request that carries {simulation_secret}, the "
        "secret these tests leave out or get wrong",
      ),
    ):
      for method in SECRET_TESTS:
        with self.subTest(test=method, needed=needed):
          test, store = self._test(method, declared)

          outcome = skipped(getattr(test, method))

          self.assertEqual(outcome, (NO_TRIGGER, needed))
          self.assertEqual(store.requests, [])

  def test_they_skip_on_a_store_without_the_endpoint(self):
    """A 404 to every caller is no hook at all, not a refusal."""
    for method in SECRET_TESTS:
      with self.subTest(test=method):
        test, _ = self._test(method, order_update=None)

        with flagsaver.flagsaver(simulation_secret=SECRET):
          category, detail = skipped(getattr(test, method))

        self.assertEqual(category, NO_TRIGGER)
        self.assertIn("the server has an endpoint for", detail)

  def test_a_wrong_configured_secret_skips_only_the_test_that_needs_it(self):
    """The refusals are still refusals; the right secret can't be shown."""
    for method in SECRET_TESTS[:2]:
      with self.subTest(test=method):
        test, _ = self._test(method)
        with flagsaver.flagsaver(simulation_secret="not-the-secret"):
          getattr(test, method)()

    test, _ = self._test(SECRET_TESTS[2])
    with flagsaver.flagsaver(simulation_secret="not-the-secret"):
      category, detail = skipped(getattr(test, SECRET_TESTS[2]))

    self.assertEqual(category, ENVIRONMENT)
    self.assertIn("pass --simulation_secret", detail)


if __name__ == "__main__":
  unittest.main()

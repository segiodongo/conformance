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

"""Unit tests for the known-customer tests and the credentials they send.

From 2026-08-25, identity linking says a business MUST NOT return stored
user-specific state unless the request is user-authenticated. The suite's
known-customer tests send the credentials a `known_customer` fixture
declares, and skip when a server of that release would have to answer from
what it stores for a buyer on the strength of an email. They run here
against the scripted store in `strict_store.py`, which follows the rule,
and against the same store in the sample server's posture, which goes by
the email alone. No server or fixture file is needed.
"""

import unittest

from absl.testing import flagsaver
from framework.discovery import UcpProfile
from framework.outcomes import NO_TRIGGER, is_not_applicable, skip_category
from shopping.fulfillment import fulfillment_test
from tests.strict_store import StrictStore, wired

CHECKOUT = "dev.ucp.shopping.checkout"

ADDRESSES = [
  {
    "street_address": "1 First St",
    "address_locality": "Springfield",
    "address_region": "IL",
    "postal_code": "62704",
    "address_country": "US",
  },
  {
    "street_address": "2 Second Ave",
    "address_locality": "Metropolis",
    "address_region": "NY",
    "postal_code": "10012",
    "address_country": "US",
  },
]

# What the store knows: two buyers, one of them with saved addresses.
CUSTOMERS = {
  "ada@example.com": {"token": "ada-token", "addresses": ADDRESSES},
  "bea@example.com": {"token": "bea-token", "addresses": []},
}

# The tests that expect the store to answer from what it keeps for a buyer,
# each with the buyer it acts for. (The fifth, a webhook test, needs a
# receiver and is covered by the `act_as` tests below.)
LOOKUP_TESTS = (
  ("test_known_customer_no_address", "bea"),
  ("test_known_customer_one_address", "ada"),
  ("test_known_customer_multiple_addresses_selection", "ada"),
  ("test_known_user_existing_address_reuse", "ada"),
)


def fixtures(*, credentials: bool) -> dict:
  """Return the two customer fixtures, with or without their credentials."""
  ada = {
    "full_name": "Ada Known",
    "email": "ada@example.com",
    "addresses": ADDRESSES,
  }
  bea = {"full_name": "Bea Known", "email": "bea@example.com"}
  if credentials:
    ada["headers"] = {"Authorization": "Bearer ada-token"}
    bea["headers"] = {"Authorization": "Bearer bea-token"}
  return {"known_customer": ada, "known_customer_without_address": bea}


def profile(version: str, checkout: str | None = None) -> UcpProfile:
  """Return a server profile of a release, with a checkout capability."""
  return UcpProfile(
    {
      "ucp": {
        "version": version,
        "capabilities": {CHECKOUT: [{"version": checkout or version}]},
      }
    }
  )


def fulfillment(method: str, store: StrictStore, declared: dict, version: str):
  """Return a FulfillmentTest for `method`, wired to a store and a release."""
  test = wired(fulfillment_test.FulfillmentTest, method, store, declared)
  test.server_profile = profile(version)
  return test


def authorizations(store: StrictStore) -> set:
  """Return the Authorization values of the requests the store received."""
  return {request.headers.get("authorization") for request in store.requests}


class KnownCustomerSuiteTestsTest(unittest.TestCase):
  """The known-customer tests against stores of both postures."""

  def test_they_pass_with_credentials_on_a_store_that_wants_them(self):
    """Every request carries the buyer's token, and the store answers."""
    for method, buyer in LOOKUP_TESTS:
      with self.subTest(test=method):
        store = StrictStore(customers=CUSTOMERS)
        test = fulfillment(
          method, store, fixtures(credentials=True), "2026-08-25"
        )

        getattr(test, method)()

        self.assertEqual(authorizations(store), {f"Bearer {buyer}-token"})

  def test_without_credentials_they_skip_from_2026_08_25(self):
    """An email alone is not asked to bring back stored state."""
    for method, buyer in LOOKUP_TESTS:
      with self.subTest(test=method):
        store = StrictStore(customers=CUSTOMERS)
        test = fulfillment(
          method, store, fixtures(credentials=False), "2026-08-25"
        )

        with self.assertRaises(unittest.SkipTest) as skipped:
          getattr(test, method)()

        category, detail = skip_category(str(skipped.exception))
        self.assertEqual(category, NO_TRIGGER)
        key = (
          "known_customer"
          if buyer == "ada"
          else "known_customer_without_address"
        )
        self.assertTrue(
          detail.startswith(f"{key} with headers that authenticate the user"),
          detail,
        )
        self.assertEqual(store.requests, [])

  def test_without_credentials_they_run_as_before_on_an_older_server(self):
    """Before 2026-08-25 no rule forbids the lookup by email."""
    for method, _ in LOOKUP_TESTS:
      with self.subTest(test=method):
        store = StrictStore(customers=CUSTOMERS, stored_state="email")
        test = fulfillment(
          method, store, fixtures(credentials=False), "2026-04-08"
        )

        getattr(test, method)()

        self.assertEqual(authorizations(store), {None})

  def test_a_store_that_follows_the_rule_gives_an_email_nothing(self):
    """Why the tests skip: such a store cannot pass them by email."""
    store = StrictStore(customers=CUSTOMERS)
    method = "test_known_customer_one_address"
    # An older release, so the test runs, against the stricter posture.
    test = fulfillment(method, store, fixtures(credentials=False), "2026-04-08")

    with self.assertRaises(AssertionError) as failed:
      getattr(test, method)()

    self.assertIn("Stored addresses were not injected", str(failed.exception))

  def test_a_new_address_needs_no_credentials(self):
    """A test that expects nothing stored back runs on an email alone."""
    store = StrictStore(customers=CUSTOMERS)
    method = "test_known_customer_new_address"
    test = fulfillment(method, store, fixtures(credentials=False), "2026-08-25")

    getattr(test, method)()

    self.assertEqual(authorizations(store), {None})

  def test_a_new_address_goes_with_the_credentials_when_declared(self):
    """Declared credentials are sent whether or not they are needed."""
    store = StrictStore(customers=CUSTOMERS)
    method = "test_known_customer_new_address"
    test = fulfillment(method, store, fixtures(credentials=True), "2026-08-25")

    getattr(test, method)()

    self.assertEqual(authorizations(store), {"Bearer ada-token"})


class StoredStateWithoutCredentialsTest(unittest.TestCase):
  """The test of the rule itself: an email alone brings nothing back."""

  METHOD = "test_saved_addresses_need_a_user_authenticated_request"

  def test_a_store_that_follows_the_rule_passes(self):
    """Declared or not, the customer's credentials are not sent."""
    for credentials in (True, False):
      with self.subTest(credentials=credentials):
        store = StrictStore(customers=CUSTOMERS)
        test = fulfillment(
          self.METHOD,
          store,
          fixtures(credentials=credentials),
          "2026-08-25",
        )

        getattr(test, self.METHOD)()

        self.assertEqual(authorizations(store), {None})
        self.assertGreaterEqual(len(store.requests), 2)

  def test_a_store_that_answers_an_email_fails(self):
    """The failure names the addresses that came back, and the rule."""
    store = StrictStore(customers=CUSTOMERS, stored_state="email")
    test = fulfillment(
      self.METHOD, store, fixtures(credentials=True), "2026-08-25"
    )

    with self.assertRaises(AssertionError) as failed:
      getattr(test, self.METHOD)()

    message = str(failed.exception)
    self.assertIn("1 First St", message)
    self.assertIn("2 Second Ave", message)
    self.assertIn("MUST NOT return stored user-specific state", message)

  def test_an_older_release_is_not_applicable(self):
    """Before 2026-08-25 the rule isn't stated, and nothing is sent."""
    for version in ("2026-04-08", "2026-01-23"):
      with self.subTest(version=version):
        store = StrictStore(customers=CUSTOMERS, stored_state="email")
        test = fulfillment(
          self.METHOD, store, fixtures(credentials=False), version
        )

        with self.assertRaises(unittest.SkipTest) as skipped:
          getattr(test, self.METHOD)()

        reason = str(skipped.exception)
        self.assertTrue(is_not_applicable(reason), reason)
        self.assertIn(f"the server implements {version}", reason)
        self.assertIn("introduced in 2026-08-25", reason)
        self.assertEqual(store.requests, [])

  def test_it_needs_a_customer_with_stored_addresses(self):
    """No customer, or one without addresses, is a missing trigger."""
    without_addresses = fixtures(credentials=True)
    without_addresses["known_customer"]["addresses"] = []
    for declared, needed in (
      ({}, "known_customer"),
      (without_addresses, "known_customer with stored addresses"),
    ):
      with self.subTest(needed=needed):
        test = fulfillment(self.METHOD, StrictStore(), declared, "2026-08-25")

        with self.assertRaises(unittest.SkipTest) as skipped:
          getattr(test, self.METHOD)()

        self.assertEqual(
          skip_category(str(skipped.exception)), (NO_TRIGGER, needed)
        )


class ActAsTest(unittest.TestCase):
  """`act_as` sends a customer's headers, or skips for lack of them."""

  def _probe(self, version: str | None):
    store = StrictStore(customers=CUSTOMERS)
    probe = wired(
      fulfillment_test.FulfillmentTest, "test_known_customer_one_address", store
    )
    probe.server_profile = profile(version) if version else None
    return probe, store

  def test_headers_ride_on_every_request_that_follows(self):
    """The helpers' own requests carry them too, completion included."""
    probe, store = self._probe("2026-08-25")
    customer = fixtures(credentials=True)["known_customer"]

    probe.act_as(customer)
    created = probe.create_checkout_session(buyer={"email": customer["email"]})
    probe.complete_checkout_session(created["id"])

    self.assertGreater(len(store.requests), 3)
    self.assertEqual(authorizations(store), {"Bearer ada-token"})
    self.assertEqual(probe.user_headers, {"Authorization": "Bearer ada-token"})

  def test_no_headers_and_a_2026_08_25_server_skip(self):
    """The skip names the fixture key and what it lacks."""
    for key in ("known_customer", "known_customer_without_address"):
      with self.subTest(key=key):
        probe, _ = self._probe("2026-08-25")

        with self.assertRaises(unittest.SkipTest) as skipped:
          probe.act_as(fixtures(credentials=False)[key], key=key)

        category, detail = skip_category(str(skipped.exception))
        self.assertEqual(category, NO_TRIGGER)
        self.assertEqual(
          detail,
          f"{key} with headers that authenticate the user (from 2026-08-25 "
          "a business MUST NOT return stored user state to a request that "
          "is not user-authenticated)",
        )

  def test_no_headers_run_unauthenticated_where_nothing_forbids_it(self):
    """An older release, an unknown one, or nothing stored expected."""
    customer = fixtures(credentials=False)["known_customer"]
    for version, expects_stored_state in (
      ("2026-04-08", True),
      ("2026-01-23", True),
      (None, True),
      ("2026-08-25", False),
    ):
      with self.subTest(version=version, expects=expects_stored_state):
        probe, _ = self._probe(version)

        probe.act_as(customer, expects_stored_state=expects_stored_state)

        self.assertIsNone(probe.user_headers)
        self.assertNotIn("Authorization", probe.get_headers())

  def test_the_checkout_capability_decides_the_release(self):
    """The version of the checkout capability counts, not the profile's."""
    customer = fixtures(credentials=False)["known_customer"]
    probe, _ = self._probe("2026-04-08")
    probe.server_profile = profile("2026-04-08", checkout="2026-08-25")

    with self.assertRaises(unittest.SkipTest):
      probe.act_as(customer)

    probe.server_profile = profile("2026-08-25", checkout="2026-04-08")
    probe.act_as(customer)

  def test_credential_free_headers_leave_the_users_credentials_out(self):
    """A request sent without credentials is not user-authenticated either."""
    probe, _ = self._probe("2026-08-25")
    probe.act_as(
      {
        "email": "ada@example.com",
        "headers": {"Authorization": "Bearer ada-token", "X-User": "ada"},
      }
    )

    sent = {name.lower() for name in probe.get_headers()}
    bare = {name.lower() for name in probe.credential_free_headers()}

    self.assertLessEqual({"authorization", "x-user"}, sent)
    self.assertFalse({"authorization", "x-user"} & bare)
    self.assertIn("ucp-agent", bare)

  def test_the_users_credential_replaces_the_platforms(self):
    """A user header takes the place of --auth_header's, whatever its case."""
    probe, _ = self._probe("2026-08-25")
    with flagsaver.flagsaver(auth_header="Authorization: Bearer platform"):
      self.assertEqual(probe.get_headers()["Authorization"], "Bearer platform")

      probe.act_as(
        {
          "email": "ada@example.com",
          "headers": {"authorization": "Bearer ada-token"},
        }
      )
      headers = probe.get_headers()

    self.assertEqual(headers["authorization"], "Bearer ada-token")
    self.assertNotIn("Authorization", headers)


class ServerImplementsTest(unittest.TestCase):
  """`server_implements` compares the release a server advertises."""

  def _probe(self, server_profile: UcpProfile | None):
    probe = wired(
      fulfillment_test.FulfillmentTest,
      "test_known_customer_one_address",
      StrictStore(),
    )
    probe.server_profile = server_profile
    return probe

  def test_release_dates_compare_in_order(self):
    """The release itself and later ones implement it; earlier ones don't."""
    for version, expected in (
      ("2026-01-23", False),
      ("2026-04-08", False),
      ("2026-08-25", True),
      ("2027-02-01", True),
    ):
      with self.subTest(version=version):
        probe = self._probe(profile(version))
        self.assertIs(probe.server_implements("2026-08-25", CHECKOUT), expected)

  def test_no_version_or_no_release_date_is_the_current_specification(self):
    """A server that names no release is held to the newest rules."""
    for version in ("", "draft", "v2"):
      with self.subTest(version=version):
        probe = self._probe(profile(version))
        self.assertTrue(probe.server_implements("2026-08-25", CHECKOUT))

  def test_an_unknown_profile_applies_no_version_check(self):
    """Without the server's profile nothing is known to be implemented."""
    probe = self._probe(None)
    self.assertFalse(probe.server_implements("2026-08-25", CHECKOUT))

  def test_the_capability_version_comes_before_the_profiles(self):
    """A capability the server offers decides; otherwise the profile does."""
    probe = self._probe(profile("2026-04-08", checkout="2026-08-25"))

    self.assertTrue(probe.server_implements("2026-08-25", CHECKOUT))
    self.assertFalse(
      probe.server_implements("2026-08-25", "dev.ucp.shopping.order")
    )
    self.assertFalse(probe.server_implements("2026-08-25"))


if __name__ == "__main__":
  unittest.main()

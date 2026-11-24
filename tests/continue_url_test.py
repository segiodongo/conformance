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

"""Unit tests for the `continue_url` tests on a server reached over local HTTP.

CHECKOUT-0094 has `continue_url` be an absolute HTTPS URL. A server on this
machine, reached over `http://localhost` by the run's own arrangement,
builds the URL from that address and so cannot show the rule: the test of
the rule skips as an environment limit, and the test that a
`requires_escalation` checkout provides the URL takes it as provided. The
two suite tests run here against the scripted store in `strict_store.py`,
which returns a `continue_url` built from whatever address it is given. No
server or fixture file is needed.
"""

import unittest

from framework.outcomes import (
  ENVIRONMENT,
  NO_TRIGGER,
  NOT_EXERCISED,
)
from shopping.checkout import lifecycle_test
from tests.strict_store import StrictStore, failure, skipped, wired

FORMAT_TEST = "test_continue_url_is_absolute_https_url"
ESCALATION_TEST = "test_requires_escalation_includes_continue_url"

# Where the run reaches the server under test (--server_url).
LOCAL_HTTP = "http://localhost:8182"
DEPLOYED = "https://store.example"


def suite_test(method, server_url, **store_options):
  """Return a lifecycle test for a server reached at `server_url`."""
  store = StrictStore(
    **{"needs_buyer": False, "escalation_items": {"needs_buyer_step"}}
    | store_options
  )
  test = wired(
    lifecycle_test.CheckoutLifecycleTest,
    method,
    store,
    {"escalation_item_sku": "needs_buyer_step"},
  )
  # What setUp would take from --server_url.
  test.base_url = server_url
  return test


class ContinueUrlFormatTest(unittest.TestCase):
  """`test_continue_url_is_absolute_https_url`, by where the server is."""

  def _test(self, server_url, continue_url):
    return getattr(
      suite_test(FORMAT_TEST, server_url, continue_url=continue_url),
      FORMAT_TEST,
    )

  def test_an_https_url_passes_wherever_the_server_is(self):
    """The rule is checked, and met."""
    for server_url in (DEPLOYED, LOCAL_HTTP):
      with self.subTest(server_url=server_url):
        self._test(server_url, "https://store.example/checkout")()

  def test_a_local_http_url_from_a_local_http_server_skips(self):
    """Not a pass: the scheme the rule is about could not be checked."""
    for continue_url in (
      "http://localhost:8182/checkout",
      "http://127.0.0.1:8182/checkout",
    ):
      with self.subTest(continue_url=continue_url):
        category, detail = skipped(self._test(LOCAL_HTTP, continue_url))

        self.assertEqual(category, ENVIRONMENT)
        self.assertEqual(
          detail,
          "the server is reached over http on a local address "
          f"(http://localhost:8182) and its continue_url is on one too "
          f"({continue_url}/chk_1), so the HTTPS the URL MUST use can't be "
          "checked; serve the server over HTTPS (or test a deployed "
          "instance) to check it",
        )

  def test_a_local_http_url_from_a_deployed_server_fails(self):
    """A server reached over HTTPS has no such excuse."""
    message = failure(self._test(DEPLOYED, "http://localhost:8182/checkout"))

    self.assertIn("URL scheme must be 'https'", message)
    self.assertIn("(CHECKOUT-0094)", message)

  def test_plain_http_on_another_host_fails_a_local_server_too(self):
    """Only a URL on a local address is the run's own arrangement."""
    message = failure(self._test(LOCAL_HTTP, "http://store.example/checkout"))

    self.assertIn("URL scheme must be 'https'", message)

  def test_another_scheme_on_a_local_address_fails(self):
    """Plain HTTP is what the run arranged; nothing else is let through."""
    message = failure(self._test(LOCAL_HTTP, "ftp://localhost/checkout"))

    self.assertIn("URL scheme must be 'https', got 'ftp'", message)

  def test_no_continue_url_is_not_exercised_as_before(self):
    """The URL is optional on a checkout that isn't escalated."""
    for server_url in (DEPLOYED, LOCAL_HTTP):
      with self.subTest(server_url=server_url):
        category, detail = skipped(self._test(server_url, None))

        self.assertEqual(category, NOT_EXERCISED)
        self.assertIn("omitted optional continue_url", detail)


class EscalationContinueUrlTest(unittest.TestCase):
  """`test_requires_escalation_includes_continue_url`, the same way."""

  def _test(self, server_url, continue_url, **store_options):
    return getattr(
      suite_test(
        ESCALATION_TEST,
        server_url,
        continue_url=continue_url,
        **store_options,
      ),
      ESCALATION_TEST,
    )

  def test_an_https_url_passes_wherever_the_server_is(self):
    """The checkout is escalated and provides the URL."""
    for server_url in (DEPLOYED, LOCAL_HTTP):
      with self.subTest(server_url=server_url):
        self._test(server_url, "https://store.example/checkout")()

  def test_a_local_http_url_from_a_local_http_server_passes(self):
    """The rules cited are about providing the URL, and it is provided."""
    self._test(LOCAL_HTTP, "http://localhost:8182/checkout")()

  def test_a_local_http_url_from_a_deployed_server_fails(self):
    """As before: the URL provided has to be a usable one."""
    message = failure(self._test(DEPLOYED, "http://localhost:8182/checkout"))

    self.assertIn("URL scheme must be 'https'", message)
    self.assertIn("(CHECKOUT-0091, CHECKOUT-0107)", message)

  def test_no_continue_url_fails_wherever_the_server_is(self):
    """Providing none is what the rules forbid."""
    for server_url in (DEPLOYED, LOCAL_HTTP):
      with self.subTest(server_url=server_url):
        message = failure(self._test(server_url, None))

        self.assertIn("MUST provide 'continue_url'", message)

  def test_a_checkout_that_is_not_escalated_fails(self):
    """The fixtures said this item escalates; it didn't."""
    message = failure(
      self._test(
        LOCAL_HTTP, "http://localhost:8182/checkout", escalation_items=set()
      )
    )

    self.assertIn("expected 'requires_escalation'", message)

  def test_without_the_trigger_it_skips_as_before(self):
    """Nothing here changes what the test needs declared."""
    test = wired(
      lifecycle_test.CheckoutLifecycleTest, ESCALATION_TEST, StrictStore()
    )
    test.base_url = LOCAL_HTTP

    self.assertEqual(
      skipped(getattr(test, ESCALATION_TEST)),
      (NO_TRIGGER, "escalation_item_sku"),
    )


class ReachedOverLocalHttpTest(unittest.TestCase):
  """`reached_over_local_http` reads the address the run uses."""

  def test_only_plain_http_on_this_machine_counts(self):
    """An HTTPS server on this machine can show the rule, and must."""
    for server_url, expected in (
      ("http://localhost:8182", True),
      ("http://127.0.0.1:8182", True),
      ("http://[::1]:8182", True),
      ("http://shop.localhost", True),
      ("https://localhost:8443", False),
      ("http://store.example", False),
      ("https://store.example", False),
    ):
      with self.subTest(server_url=server_url):
        test = suite_test(FORMAT_TEST, server_url)

        self.assertIs(test.reached_over_local_http(), expected)


if __name__ == "__main__":
  unittest.main()

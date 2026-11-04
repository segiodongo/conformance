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

"""Shopping service discovery and version negotiation tests.

The vertical-agnostic half of protocol conformance lives in
`core/protocol_test.py`. This module covers the assertions that only apply
to a server advertising the shopping vertical.
"""

import uuid

from absl.testing import absltest
from framework.base_test import DEFAULT_UCP_VERSION
from framework.decorators import (
  outside_catalog,
  requires_capability,
  spec_assert,
)
from shopping.base import (
  ShoppingIntegrationTestBase,
  get_valid_payment_payload,
  rest_entry,
)
from ucp_sdk.models.schemas.shopping import checkout as checkout

try:
  from ucp_sdk.models.schemas.shopping.payment import (
    Payment,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types.payment import (
    Payment,
  )

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


@requires_capability("dev.ucp.shopping.checkout")
class ShoppingProtocolTest(ShoppingIntegrationTestBase):
  """Shopping service advertisement and version negotiation.

  Validated Paths:
  - GET /.well-known/ucp, or the profile at --discovery_url
  - POST /checkout-sessions
  """

  def _discovery_profile(self) -> dict:
    """Fetch and unwrap the discovery profile."""
    response = self.client.get(self.discovery_url)
    self.assert_response_status(response, 200)
    profile = response.json()
    return profile.get("ucp", profile)

  def _shopping_service(self, ucp_data: dict) -> dict:
    """Return the advertised shopping service entry.

    That is the REST entry if there is one, else the first entry, so that
    the transport checks report a profile with no REST entry.
    """
    shopping_services = ucp_data.get("services", {}).get("dev.ucp.shopping")
    self.assertTrue(
      shopping_services, "Shopping service not found in discovery"
    )
    entries = (
      shopping_services
      if isinstance(shopping_services, list)
      else [shopping_services]
    )
    return rest_entry(entries) or entries[0]

  @outside_catalog(
    "overview/index.md: the advertised shopping service, with its "
    "version, REST transport and endpoint (core discovery rules)"
  )
  def test_shopping_service_advertisement(self):
    """Test that the shopping service is advertised correctly.

    Given the UCP server implements the shopping vertical,
    When the discovery profile is fetched,
    Then a REST shopping service must be advertised with a date-based
    version matching the profile version and a resolvable endpoint.
    """
    ucp_data = self._discovery_profile()
    shopping_service = self._shopping_service(ucp_data)

    declared_version = ucp_data.get("version")
    expected_version = (
      declared_version.get("root", "")
      if isinstance(declared_version, dict)
      else str(declared_version)
    )
    if not expected_version:
      expected_version = self.conformance_config.get(
        "ucp_version", DEFAULT_UCP_VERSION
      )

    # The shopping service entry's version follows the same input-driven
    # rule as the profile version.
    self.assertRegex(
      str(shopping_service.get("version")),
      r"^\d{4}-\d{2}-\d{2}$",
      msg="Shopping service version must be date-based (YYYY-MM-DD)",
    )
    self.assertEqual(shopping_service.get("version"), expected_version)
    self.assertEqual(
      shopping_service.get("transport"),
      "rest",
      "Shopping service must advertise the REST transport",
    )
    self.assertIsNotNone(shopping_service.get("endpoint"))

  @outside_catalog(
    "overview/index.md, Request-Time Validation: a request with an "
    "unsupported version MUST get a version_unsupported error"
  )
  def test_version_negotiation(self):
    """Test protocol version negotiation via headers.

    Given a checkout creation request,
    When the request includes a 'UCP-Agent' header with a compatible version,
    then the request succeeds (200/201).
    When the request includes a 'UCP-Agent' header with an incompatible
    version, then the request fails with 422 Unprocessable Content.
    """
    ucp_data = self._discovery_profile()
    shopping_service = self._shopping_service(ucp_data)
    self.assertEqual(
      shopping_service.get("transport"),
      "rest",
      "Shopping service must advertise the REST transport",
    )
    self.assertIsNotNone(
      shopping_service.get("endpoint"),
      "Endpoint not found for shopping service",
    )
    checkout_sessions_url = (
      f"{str(shopping_service.get('endpoint')).rstrip('/')}/checkout-sessions"
    )

    create_payload = self.create_checkout_payload()

    # 1. Compatible Version — use the version the server itself advertises in
    # discovery, so the test stays correct across spec releases instead of
    # pinning a literal that goes stale.
    advertised = ucp_data.get("version")
    advertised_version = (
      advertised.get("root", "")
      if isinstance(advertised, dict)
      else str(advertised)
    )
    self.assertTrue(
      advertised_version, "Discovery profile must advertise a UCP version"
    )
    headers = self.get_headers()
    headers["UCP-Agent"] = f'profile="..."; version="{advertised_version}"'
    response = self.client.post(
      checkout_sessions_url,
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=headers,
    )
    self.assert_response_status(response, [200, 201])

    # 2. Incompatible Version
    headers["UCP-Agent"] = 'profile="..."; version="2099-01-01"'
    response = self.client.post(
      checkout_sessions_url,
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=headers,
    )
    self.assert_response_status(response, 422)

  # ── checkout/rest.md, Protocol Fundamentals ─────────────────────────────
  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0241",  # request and response bodies: valid JSON
  )
  def test_response_bodies_are_strict_json(self) -> None:
    """Every checkout response body is valid RFC 8259 JSON served as JSON.

    Create, Get, Update, Complete and Cancel Checkout, the error for an
    unknown session, and the error for a request whose body is not JSON:
    each body must be UTF-8 without a byte order mark, parse under the
    RFC 8259 grammar (no NaN or Infinity, no trailing commas) and come as
    application/json. The request half of the rule binds the platform; the
    malformed request shows the business does not take a body that is not
    JSON for one, and answers in JSON itself.
    """
    sessions = self.get_shopping_url("/checkout-sessions")
    create_payload = self.create_checkout_payload().model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    create = self.client.post(
      sessions, json=create_payload, headers=self.get_headers()
    )
    self.assert_response_status(create, [200, 201])
    self.assert_strict_json(create, f"Create Checkout ({create.status_code})")
    checkout_id = create.json()["id"]

    read = self.client.get(
      f"{sessions}/{checkout_id}", headers=self.get_headers()
    )
    self.assert_response_status(read, 200)
    self.assert_strict_json(read, "Get Checkout (200)")

    checkout_obj = checkout.Checkout(**read.json())
    update = self.client.put(
      f"{sessions}/{checkout_id}",
      json=self.checkout_update_payload(checkout_obj),
      headers=self.get_headers(),
    )
    self.assert_response_status(update, 200)
    self.assert_strict_json(update, "Update Checkout (200)")

    cancel = self.client.post(
      f"{sessions}/{checkout_id}/cancel", headers=self.get_headers()
    )
    self.assert_strict_json(cancel, f"Cancel Checkout ({cancel.status_code})")

    # A completion, on a second session: the response with the order in it.
    completed_id = self.create_checkout_session()["id"]
    complete = self.client.post(
      f"{sessions}/{completed_id}/complete",
      json=get_valid_payment_payload(),
      headers=self.get_headers(),
    )
    self.assert_response_status(complete, 200)
    self.assert_strict_json(complete, "Complete Checkout (200)")

    unknown = self.client.get(
      f"{sessions}/does-not-exist-{uuid.uuid4()}", headers=self.get_headers()
    )
    self.assert_strict_json(
      unknown, f"Get Checkout for an unknown session ({unknown.status_code})"
    )

    malformed = self.client.post(
      sessions, content=b'{"line_items": [', headers=self.get_headers()
    )
    self.assertTrue(
      400 <= malformed.status_code < 500,
      "a request whose body is not valid JSON must be rejected with a 4xx "
      f"(Content Types: request bodies MUST be valid JSON), got "
      f"{malformed.status_code}",
    )
    self.assert_strict_json(
      malformed, f"the error for a malformed body ({malformed.status_code})"
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0242",  # HTTPS with minimum TLS version 1.3
  )
  def test_endpoints_are_served_over_tls_1_3(self) -> None:
    """The profile and the checkout endpoint speak TLS 1.3 and nothing older.

    A TLS 1.3 handshake must complete and a handshake capped at TLS 1.2
    must be refused. A plaintext endpoint on a local address skips as an
    environment limit; anywhere else it fails.
    """
    self.assert_tls_minimum_1_3(
      [self.discovery_url, self.get_shopping_url("/checkout-sessions")]
    )

  # ── checkout/embedded.md, Discovery: Per-Checkout Configuration ─────────
  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0152",  # embedded binding with config.delegate
  )
  def test_embedded_checkout_response_includes_config_delegate(self) -> None:
    """Checkout response includes embedded binding with `config.delegate`.

    Per `checkout/embedded.md` (Per-Checkout Configuration):
    - Businesses MUST include an embedded service binding with `config.delegate`
      in checkout responses to indicate ECP availability and allowed delegations
      for a specific session (`REQ-SHOPPING-CHECKOUT-0152`).
    """
    self.require_embedded_shopping()
    checkout_data = self.create_checkout_session()
    self.assert_embedded_service_binding(
      checkout_data, rule_id="CHECKOUT-0152", where="Create Checkout"
    )


if __name__ == "__main__":
  absltest.main()

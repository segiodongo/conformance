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

"""Order tests for the UCP SDK Server."""

import datetime
import uuid
from absl import flags
from absl.testing import absltest
from framework.decorators import (
  requires_capability,
  scenario,
  spec_assert,
)
from pydantic import AnyUrl, ValidationError
from shopping.base import get_valid_payment_payload
from shopping.base import shopping_test_data
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import order

try:
  from ucp_sdk.models.schemas.shopping.payment import (
    Payment,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types.payment import (
    Payment,
  )
from ucp_sdk.models.schemas.shopping.types import adjustment
from ucp_sdk.models.schemas.shopping.types import fulfillment_event

try:
  from ucp_sdk.models.schemas.shopping.types import total
except ImportError:
  from ucp_sdk.models.schemas.common.types import total

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})

FLAGS = flags.FLAGS


@requires_capability("dev.ucp.shopping.order")
class OrderTest(ShoppingIntegrationTestBase):
  """Tests for order management.

  Validated Paths:
  - GET /orders/{id}
  - PUT /orders/{id}
  """

  @spec_assert(
    "REQ-SHOPPING-ORDER-0008",  # full order entity on every response
  )
  def test_order_retrieval(self) -> None:
    """Get Order returns the full order entity for the placed checkout.

    Given a completed checkout,
    When a GET request is sent to /orders/{id},
    Then the response is 200 OK with an order that validates against the
    order schema and carries the checkout it was placed from: its ID, its
    currency, each of its lines and its total (order.md Operations: the
    full order entity on every response). The completion response's
    ``order`` is an order confirmation, identifiers only, so it is not
    compared; the rule's other response, event delivery, is compared with
    Get Order by the webhook structure tests.
    """
    checkout_data = self.create_checkout_session()
    checkout_id = checkout_data["id"]
    complete_data = self.complete_checkout_session(checkout_id)
    order_id = complete_data["order"]["id"]

    response = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(response, 200)
    order_json = response.json()
    try:
      order_data = order.Order(**order_json)
    except ValidationError as error:
      self.fail(f"Get Order is not a valid order entity: {error}")
    self.assertEqual(order_data.id, order_id, "Order ID mismatch")
    self.assertTrue(order_data.line_items, "Get Order returned no line items")
    self.assert_order_carries_checkout(order_json, complete_data)

  # ── order/rest.md, Protocol Fundamentals and Conformance ────────────────
  # ── order.md, Get Order: Authorization ───────────────────────────────────
  @spec_assert(
    "REQ-SHOPPING-ORDER-0009",  # MUST authenticate requests to order data
    "REQ-SHOPPING-ORDER-0015",  # the same rule, in the Guidelines
  )
  def test_order_read_requires_authentication(self) -> None:
    """Order data is returned only to an authenticated caller.

    order.md: the business MUST authenticate requests to order data before
    returning a response, by any supported mechanism (API key, OAuth 2.0,
    mutual TLS or HTTP Message Signatures), and MAY allow platform
    credentials to read the orders the platform originated.

    Given an order the suite placed,
    When Get Order is sent with the transport headers but no credential at
    all: no RFC 9421 signature, no --auth_header, nothing but UCP-Agent
    naming the platform,
    Then the response is a 4xx that carries no order data; and when the
    same request is sent as the suite normally sends it, signed with the
    platform key the agent profile publishes (plus --auth_header when
    given), the order comes back, so the refusal was authentication and
    not a broken endpoint.
    """
    order_id = self.create_completed_order()
    url = self.get_order_url(order_id)

    anonymous = self.client.get(
      url, headers=self.credential_free_headers(), auth=None
    )
    self.assertTrue(
      400 <= anonymous.status_code < 500,
      "Get Order answered an unauthenticated request with "
      f"{anonymous.status_code}; order.md: the business MUST authenticate "
      "requests to order data before returning a response (a 401 or 403, "
      "or a 404 that reveals nothing, is expected)",
    )
    self.assertNotIn(
      order_id,
      anonymous.text,
      "Get Order returned the order's data to an unauthenticated request "
      f"(HTTP {anonymous.status_code}); order.md: authenticate before "
      "returning a response",
    )

    authenticated = self.client.get(url, headers=self.get_headers())
    self.assert_response_status(authenticated, 200)
    self.assertEqual(
      authenticated.json().get("id"),
      order_id,
      "the platform's own signed request did not get its order back",
    )

  @spec_assert(
    "REQ-SHOPPING-ORDER-0034",  # all response bodies MUST be valid JSON
  )
  def test_response_bodies_are_strict_json(self) -> None:
    """Every order response body is valid RFC 8259 JSON served as JSON.

    Get Order for a placed order, and the error for an unknown order: each
    body must be UTF-8 without a byte order mark, parse under the RFC 8259
    grammar (no NaN or Infinity, no trailing commas) and come as
    application/json.
    """
    order_id = self.create_completed_order()
    read = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(read, 200)
    self.assert_strict_json(read, "Get Order (200)")

    unknown = self.client.get(
      self.get_order_url(f"does-not-exist-{uuid.uuid4()}"),
      headers=self.get_headers(),
    )
    self.assert_strict_json(
      unknown, f"Get Order for an unknown order ({unknown.status_code})"
    )

  @spec_assert(
    "REQ-SHOPPING-ORDER-0035",  # HTTPS with minimum TLS version 1.3
    "REQ-SHOPPING-ORDER-0039",  # the same rule, in the Conformance section
  )
  def test_endpoints_are_served_over_tls_1_3(self) -> None:
    """The profile and the order endpoint speak TLS 1.3 and nothing older.

    A TLS 1.3 handshake must complete and a handshake capped at TLS 1.2
    must be refused. A plaintext endpoint on a local address skips as an
    environment limit; anywhere else it fails.
    """
    self.assert_tls_minimum_1_3(
      [self.discovery_url, self.get_shopping_url("/orders")]
    )

  @scenario(
    "the order's first expectation reuses the selected option's title as "
    "its description, a sample server choice that no rule requires"
  )
  def test_order_fulfillment_retrieval(self) -> None:
    """Test that fulfillment expectations are persisted in the order.

    Given a checkout session where a fulfillment option is selected,
    When the checkout is completed,
    Then the resulting order should contain fulfillment expectations
    matching the selected option.
    """
    checkout_data = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**checkout_data)
    checkout_id = checkout_obj.id

    # Update with Address to get options
    # Use helper to get a valid address from CSV
    address_data = shopping_test_data.addresses[0]
    fulfillment_address = {
      "id": "dest_manual",
      "type": "shipping_address",
      "full_name": "Jane Doe",
      "street_address": address_data["street_address"],
      "address_locality": address_data["city"],
      "address_region": address_data["state"],
      "postal_code": address_data["postal_code"],
      "address_country": address_data["country"],
    }

    fulfillment_payload = {
      "methods": [
        {
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "type": "shipping",
          "destinations": [fulfillment_address],
          "selected_destination_id": "dest_manual",
        }
      ]
    }

    update_payload = {
      "id": checkout_id,
      "currency": checkout_obj.currency,
      "line_items": [
        {
          "item": {"id": li.item.id, "title": li.item.title},
          "quantity": li.quantity,
          "id": li.id,
        }
        for li in checkout_obj.line_items
      ],
      "payment": get_valid_payment_payload()["payment"],
      "fulfillment": fulfillment_payload,
    }

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    checkout.Checkout(**response.json())

    # Check options in hierarchical structure
    checkout_json = response.json()
    options = []
    group_info = {}
    if checkout_json.get("fulfillment"):
      ful = checkout_json["fulfillment"]
      methods = ful.get("root", ful).get("methods", [])
      if methods and methods[0].get("groups"):
        group_info = methods[0]["groups"][0]
        options = group_info.get("options", [])

    self.assertTrue(options, "No options returned")

    # Select Option
    option_id = options[0]["id"]

    # Update payload to select option
    # Need to preserve the method structure
    update_payload["fulfillment"]["methods"][0]["groups"] = [
      {
        "id": group_info.get("id", "group_1"),
        "line_item_ids": group_info.get(
          "line_item_ids", [checkout_obj.line_items[0].id]
        ),
        "selected_option_id": option_id,
      }
    ]

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    # Complete
    complete_data = self.complete_checkout_session(checkout_id)
    order_id = complete_data["order"]["id"]

    # Get Order and verify fulfillment details
    response = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(response, 200)
    order_obj = order.Order(**response.json())

    self.assertTrue(
      order_obj.fulfillment.expectations, "No expectations in order"
    )
    # Verify the expectation description matches the selected option title
    self.assertEqual(
      order_obj.fulfillment.expectations[0].description,
      options[0]["title"],
      "Expectation description mismatch",
    )

  @scenario(
    "adds a shipped event through PUT /orders/{id}, the sample server's own "
    "endpoint; the spec defines only GET /orders/{id}"
  )
  def test_order_update(self) -> None:
    """Test updating an existing order.

    Given a completed order,
    When a PUT request is sent to /orders/{id} with updated fulfillment events
    (e.g., adding shipment info),
    Then the response should be 200 OK and the retrieved order should reflect
    the new event.
    """
    checkout_data = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**checkout_data)
    checkout_id = checkout_obj.id

    # Update with Address to get options
    # Use helper to get a valid address from CSV
    address_data = shopping_test_data.addresses[0]
    addr = {
      "id": "dest_manual_2",
      "type": "shipping_address",
      "full_name": "Jane Doe",
      "street_address": address_data["street_address"],
      "address_locality": address_data["city"],
      "address_region": address_data["state"],
      "postal_code": address_data["postal_code"],
      "address_country": address_data["country"],
    }

    fulfillment_payload = {
      "methods": [
        {
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "type": "shipping",
          "destinations": [addr],
          "selected_destination_id": "dest_manual_2",
        }
      ]
    }

    update_payload = {
      "id": checkout_id,
      "currency": checkout_obj.currency,
      "line_items": [
        {
          "item": {"id": li.item.id, "title": li.item.title},
          "quantity": li.quantity,
          "id": li.id,
        }
        for li in checkout_obj.line_items
      ],
      "payment": get_valid_payment_payload()["payment"],
      "fulfillment": fulfillment_payload,
    }

    resp = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(resp, 200)

    checkout_resp = resp.json()
    options = []
    group_info = {}
    if checkout_resp.get("fulfillment"):
      ful = checkout_resp["fulfillment"]
      methods = ful.get("root", ful).get("methods", [])
      if methods and methods[0].get("groups"):
        group_info = methods[0]["groups"][0]
        options = group_info.get("options", [])

    self.assertTrue(options)

    # Select option
    update_payload["fulfillment"]["methods"][0]["groups"] = [
      {
        "id": group_info.get("id", "group_1"),
        "line_item_ids": group_info.get(
          "line_item_ids", [checkout_obj.line_items[0].id]
        ),
        "selected_option_id": options[0]["id"],
      }
    ]

    self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )

    # Complete
    complete_data = self.complete_checkout_session(checkout_id)
    order_id = complete_data["order"]["id"]

    # Get Order
    resp = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(resp, 200)
    order_obj = order.Order(**resp.json())

    # Update Order (Add Shipment Event)
    new_event = fulfillment_event.FulfillmentEvent(
      id=f"evt_{uuid.uuid4()}",
      occurred_at=datetime.datetime.now(datetime.timezone.utc),
      type="shipped",
      line_items=[
        fulfillment_event.LineItem(id=li.id, quantity=li.quantity.total)
        for li in order_obj.line_items
      ],
      tracking_number="TRACK123",
      tracking_url=AnyUrl("http://track.me/123"),
      description="Shipped via FedEx",
    )

    if order_obj.fulfillment.events is None:
      order_obj.fulfillment.events = []
    order_obj.fulfillment.events.append(new_event)

    resp = self.client.put(
      self.get_order_url(order_id),
      json=order_obj.model_dump(mode="json", by_alias=True, exclude_none=True),
      headers=self.get_headers(),
    )
    self.assert_response_status(resp, 200)

    updated_order = order.Order(**resp.json())
    self.assertTrue(updated_order.fulfillment.events, "No events returned")
    self.assertEqual(
      updated_order.fulfillment.events[0].tracking_number,
      "TRACK123",
      msg="Order event not persisted",
    )

  @scenario(
    "adds a refund adjustment through PUT /orders/{id}, the sample server's "
    "own endpoint; the spec defines only GET /orders/{id}"
  )
  def test_order_adjustments(self) -> None:
    """Test that order adjustments are persisted.

    Given a completed order,
    When the order is updated to include an adjustment (e.g., refund),
    Then the retrieved order should correctly retain the adjustment.
    """
    order_id = self.create_completed_order()

    # Get Order
    resp = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(resp, 200)
    order_obj = order.Order(**resp.json())

    # Add Adjustment
    adj = adjustment.Adjustment(
      id=f"adj_{uuid.uuid4()}",
      type="refund",
      occurred_at=datetime.datetime.now(datetime.timezone.utc),
      status="pending",
      totals=[total.Total(type="total", amount=-500)],
      description="Customer refund request",
    )

    if order_obj.adjustments is None:
      order_obj.adjustments = []
    order_obj.adjustments.append(adj)

    # Update Order
    resp = self.client.put(
      self.get_order_url(order_id),
      json=order_obj.model_dump(mode="json", by_alias=True, exclude_none=True),
      headers=self.get_headers(),
    )
    self.assert_response_status(resp, 200)

    updated_order = order.Order(**resp.json())
    self.assertTrue(updated_order.adjustments, "No adjustments returned")
    self.assertEqual(updated_order.adjustments[0].totals[0].amount, -500)
    self.assertEqual(updated_order.adjustments[0].type, "refund")

  @spec_assert(
    "REQ-SHOPPING-ORDER-0006",  # settlement adjustment carries settled measure
    "REQ-SHOPPING-ORDER-0007",  # unit identity matches pricing basis, qty 0
  )
  def test_measurement_price_settlement_adjustment_carries_settled_measure(
    self,
  ) -> None:
    """Measurement settlement carries `measure` matching pricing basis."""
    sku = self.require_trigger("settlement_item_sku")
    checkout_data = self.create_checkout_session(item_id=sku)
    complete_data = self.complete_checkout_session(checkout_data["id"])
    order_id = complete_data["order"]["id"]

    resp = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(resp, 200)
    self.assert_order_measurement_settlement(
      resp.json(), where=f"GET /orders/{order_id} ({sku!r})"
    )


if __name__ == "__main__":
  absltest.main()

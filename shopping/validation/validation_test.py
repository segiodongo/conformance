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

"""Validation tests for the UCP SDK Server."""

from absl.testing import absltest
from framework.decorators import requires_capability, spec_assert
from shopping.base import get_valid_payment_payload
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import (
  checkout_update_request as checkout_update_req,
)
from ucp_sdk.models.schemas.shopping import checkout as checkout

try:
  from ucp_sdk.models.schemas.shopping import payment_update_request
except ImportError:
  from ucp_sdk.models.schemas.common.types import payment_update_request
try:
  from ucp_sdk.models.schemas.shopping.payment import (
    Payment,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types.payment import (
    Payment,
  )
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request

try:
  from ucp_sdk.models.schemas.shopping.types.error_response import ErrorResponse
except ImportError:
  from ucp_sdk.models.schemas.common.types.error_response import ErrorResponse
from pydantic import ValidationError


# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


def _explains_line(message: dict) -> bool:
  """Whether a messages[] entry speaks to a line item or its quantity.

  checkout.md asks for "an explanatory messages[] entry" without fixing its
  type, code or path, so any entry that points into ``line_items`` or whose
  code or content mentions quantity or stock is taken as the explanation.

  Args:
    message: One ``messages[]`` entry as returned by the server.

  Returns:
    True when the entry can be read as explaining a quantity revision.

  """
  path = str(message.get("path") or "")
  if "line_items" in path:
    return True
  words = f"{message.get('code') or ''} {message.get('content') or ''}"
  return any(word in words.casefold() for word in ("quantity", "stock"))


@requires_capability("dev.ucp.shopping.checkout")
class ValidationTest(ShoppingIntegrationTestBase):
  """Tests for input validation and error handling.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - POST /checkout-sessions/{id}/complete
  """

  def assert_business_error(
    self,
    response,
    accepted_codes: set[str],
    error_4xx_substring: str,
  ) -> None:
    """Assert a business-level failure in either posture the spec permits.

    The spec models business failures in-band: checkout-rest.md's own
    "Error Response" example answers an all-items-out-of-stock create with
    HTTP 200 and ``ucp.status: "error"`` plus a typed ``messages[]`` entry,
    and partial failures ride as error messages on a created resource. A
    transport-level 4xx rejection (the posture this repo's Flower Shop
    reference implements) is also accepted. Each posture is validated
    strictly:

    - 4xx: the body must describe the error (same substring assertion the
      tests always made against the reference server).
    - 200/201: ``messages[]`` must contain at least one ``type: "error"``
      entry carrying the full message envelope (type, code, content,
      severity — required by message_error.json), and at least one such
      entry must use an accepted standardized code (checkout.md error-code
      table / error_code.json examples). Two in-band error shapes are
      accepted:

      - resourceless (no ``id``): the ``error_response`` shape for
        operations that could not establish a resource — ``ucp.status``
        must be ``"error"`` (checkout.md: "no resource is included in the
        response body");
      - resource-bearing (``id`` present): the failure rides as
        ``messages[]`` on the checkout resource itself, the spec's shape
        when the session already exists (checkout.md status values;
        checkout-rest.md's create example answers a missing required field
        with ``status: "incomplete"`` plus a typed message) — the checkout
        must not have completed (``status`` != ``"completed"``, no
        ``order``).
    """
    if 400 <= response.status_code < 500:
      self._assert_structured_4xx_error(response, error_4xx_substring)
      return

    self.assert_response_status(response, [200, 201])
    data = response.json()
    errors = [m for m in data.get("messages", []) if m.get("type") == "error"]
    self.assertTrue(
      errors,
      "Business failure answered with 2xx must carry an in-band "
      "messages[] entry of type 'error' (checkout.md error handling)",
    )
    for message in errors:
      for field in ("type", "code", "content", "severity"):
        self.assertTrue(
          message.get(field),
          f"Error message missing required field '{field}' "
          f"(message envelope): {message}",
        )
    codes = {m.get("code") for m in errors}
    self.assertTrue(
      codes & accepted_codes,
      f"Expected an error code in {sorted(accepted_codes)}, got "
      f"{sorted(codes)}",
    )
    if data.get("id") is None:
      # Resourceless error_response shape: no resource was established.
      ucp_envelope = data.get("ucp") or {}
      self.assertEqual(
        ucp_envelope.get("status"),
        "error",
        "Expected ucp.status to be 'error' for a resourceless "
        "business-failure response (checkout.md: 'no resource is "
        "included in the response body')",
      )
    else:
      # Resource-bearing shape: the session exists and the failure rides
      # as messages[] on the checkout resource. ucp.status is the shape
      # discriminator (checkout.md): "error" means error information is
      # returned INSTEAD of a resource, so a resource-bearing answer must
      # not claim it (absent defaults to "success" per ucp.json base).
      ucp_envelope = data.get("ucp") or {}
      self.assertEqual(
        ucp_envelope.get("status", "success"),
        "success",
        "A response carrying a checkout resource must not set "
        "ucp.status 'error' (checkout.md: an error-status response "
        "includes no resource)",
      )
      # The failed operation must leave the checkout in a valid,
      # non-completed state (checkout.json status enum).
      self.assertIn(
        data.get("status"),
        {
          "incomplete",
          "requires_escalation",
          "ready_for_complete",
          "complete_in_progress",
          "canceled",
        },
        "A checkout carrying an in-band business-failure message must "
        "report a valid, non-'completed' status",
      )
      self.assertIsNone(
        data.get("order"),
        "A checkout carrying an in-band business-failure message must "
        "not carry an order",
      )

  def _assert_structured_4xx_error(self, response, substring: str) -> None:
    """Assert that a 4xx response is structured correctly (UCP or legacy)."""
    try:
      data = response.json()
      if "messages" in data:
        try:
          error_resp = ErrorResponse(**data)
          errors = [m for m in error_resp.messages if m.type == "error"]
          self.assertTrue(
            errors,
            "Compliant 4xx response must have at least one error message",
          )
          self.assertTrue(
            any(substring.lower() in e.content.lower() for e in errors),
            f"Expected '{substring}' in error messages",
          )
        except ValidationError as e:
          self.fail(f"Failed to parse ErrorResponse: {e}")
      else:
        self.assertTrue(
          data.get("detail"),
          "Error response missing 'detail' or 'messages' field",
        )
        self.assertIn(
          substring.lower(),
          str(data["detail"]).lower(),
        )
    except ValueError:
      self.assertIn(
        substring.lower(),
        response.text.lower(),
        msg=f"Expected '{substring}' in the 4xx error body",
      )

  def assert_4xx_error(
    self,
    response,
    expected_status: int,
    substring: str,
  ) -> None:
    """Assert a 4xx rejection with a specific status and error message."""
    self.assert_response_status(response, expected_status)
    self._assert_structured_4xx_error(response, substring)

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_out_of_stock(self) -> None:
    """Test validation for out-of-stock items.

    Given a product with 0 inventory,
    When a checkout creation request is made for this item,
    Then the server either rejects with a 4xx describing the stock problem,
    or answers in-band per the spec's error model with a typed
    'out_of_stock' error message.
    """
    # Get out of stock item from config
    out_of_stock_item = self.conformance_config.get(
      "out_of_stock_item",
      {"id": "out_of_stock_item_1", "title": "Out of Stock Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=out_of_stock_item["id"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      accepted_codes={"out_of_stock", "item_unavailable"},
      error_4xx_substring="stock",
    )

  _EXCESSIVE_QUANTITY = 10001

  def _request_line_quantity(
    self, checkout_obj: checkout.Checkout, quantity: int
  ):
    """PUT the checkout with its first line's quantity set to `quantity`.

    Args:
      checkout_obj: The checkout as created.
      quantity: The quantity to request for the first line item.

    Returns:
      The raw response, whichever posture the server takes.

    """
    item_update = item_update_request.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
    )
    line_item_update = line_item_update_request.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=quantity,
    )
    payment_update = None
    if checkout_obj.payment is not None:
      payment_update = payment_update_request.PaymentUpdateRequest(
        instruments=checkout_obj.payment.instruments,
        handlers=[
          h.model_dump(mode="json", exclude_none=True)
          for h in checkout_obj.payment.instruments
        ],
      )
    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_obj.id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
    )
    return self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_update_inventory_validation(self) -> None:
    """Test that inventory validation is enforced on update.

    Given an existing checkout session with a valid quantity,
    When the line item quantity is updated to exceed available stock,
    Then the server either rejects with a 4xx describing the stock
    problem, answers in-band per the spec's error model with a typed
    'out_of_stock'/'item_unavailable' error message, or clamps the
    quantity and reports a 'quantity_adjusted' warning (checkout-rest.md
    "Business Outcomes" — the spec's canonical answer to requesting more
    units than are in stock).
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    response = self._request_line_quantity(
      checkout_obj, self._EXCESSIVE_QUANTITY
    )

    if response.status_code in (200, 201):
      data = response.json()
      messages = data.get("messages", [])
      adjusted = [
        m
        for m in messages
        if m.get("type") == "warning" and m.get("code") == "quantity_adjusted"
      ]
      if adjusted and not any(m.get("type") == "error" for m in messages):
        # Clamp-and-warn posture (checkout-rest.md "Business Outcomes"):
        # the server fulfills what it can and reports the adjustment. The
        # returned quantity must actually be clamped below the requested
        # amount — silently accepting the excess would be a real failure.
        quantities = [li.get("quantity") for li in data.get("line_items", [])]
        self.assertTrue(
          quantities
          and all(
            isinstance(q, int) and q < self._EXCESSIVE_QUANTITY
            for q in quantities
          ),
          "A quantity_adjusted warning must come with the line-item "
          "quantity actually clamped below the requested amount, got "
          f"{quantities}",
        )
        return

    self.assert_business_error(
      response,
      accepted_codes={"out_of_stock", "item_unavailable"},
      error_4xx_substring="stock",
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0010",  # a revision comes with a messages[] entry
    "REQ-SHOPPING-CHECKOUT-0011",  # MUST NOT silently reinterpret quantity
  )
  def test_quantity_revision_is_explained(self) -> None:
    """A revised line quantity comes with a messages[] entry explaining it.

    checkout.md "Quantity and sale basis": a revision MUST be returned as a
    revised line ``quantity`` with an explanatory ``messages[]`` entry, and
    the business MUST NOT silently reinterpret the requested quantity. The
    same paragraph lets the business revise a quantity for its own reasons
    such as limited stock, so this test provokes a revision by requesting
    more units than any store stocks (see findings.md, "Rule scope").

    A server that rejects the request instead (4xx, or 2xx with an in-band
    error and the quantity left as requested) or that accepts it as
    requested made no revision; both leave the rules unexercised and skip.
    When the quantity does come back lowered, some message must speak to
    that line or its quantity: a ``path`` into ``line_items``, or a code or
    content mentioning quantity or stock. Any ``type`` counts; checkout.md
    does not prescribe one.
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    line_id = checkout_obj.line_items[0].id
    response = self._request_line_quantity(
      checkout_obj, self._EXCESSIVE_QUANTITY
    )
    if response.status_code not in (200, 201):
      self.skip_not_exercised(
        f"the server rejects a quantity of {self._EXCESSIVE_QUANTITY} with "
        f"HTTP {response.status_code} rather than revising it, so no "
        "revision was made to explain"
      )
    data = response.json()
    line = next(
      (li for li in data.get("line_items", []) if li.get("id") == line_id),
      None,
    )
    if line is None:
      self.skip_not_exercised(
        "the server dropped the line rather than revising its quantity"
      )
    quantity = line.get("quantity")
    if quantity == self._EXCESSIVE_QUANTITY:
      self.skip_not_exercised(
        f"the server kept the requested quantity {quantity}, so no revision "
        "was made to explain"
      )
    self.assertIsInstance(quantity, int)
    self.assertLess(
      quantity,
      self._EXCESSIVE_QUANTITY,
      "a line quantity returned higher than requested is not a revision the "
      "spec describes",
    )
    messages = data.get("messages") or []
    explanatory = [m for m in messages if _explains_line(m)]
    self.assertTrue(
      explanatory,
      f"the line quantity was revised from {self._EXCESSIVE_QUANTITY} to "
      f"{quantity} without an explanatory messages[] entry about the line "
      f"(checkout.md: a revision MUST come with an explanatory message and "
      f"MUST NOT silently reinterpret the quantity); messages: {messages}",
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_product_not_found(self) -> None:
    """Test validation for non-existent products.

    Given a request for a product ID that does not exist in the catalog,
    When a checkout creation request is made,
    Then the server either rejects with a 4xx indicating the product was not
    found, or answers in-band per the spec's error model with a typed error
    message.
    """
    non_existent_item = self.conformance_config.get(
      "non_existent_item",
      {"id": "non_existent_item_1", "title": "Non-existent Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=non_existent_item["id"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      accepted_codes={"not_found", "item_unavailable"},
      error_4xx_substring="not found",
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_payment_failure(self) -> None:
    """Test handling of payment failures.

    Given a checkout session ready for completion,
    When a payment instrument with a known failing token ('fail_token') is
    submitted,
    Then the server either rejects with a 4xx describing the payment
    problem, or answers in-band per the spec's error model with a typed
    'payment_failed' error message (checkout.md standard error codes /
    error_code.json; checkout-rest.md "Error Responses": business
    outcomes return HTTP 200 with the UCP envelope and messages[]).
    """
    response_json = self.create_checkout_session(handlers=[])
    checkout_id = checkout.Checkout(**response_json).id

    # Use the helper to get valid structure, but request the failing instrument
    # 'instr_fail' is loaded from payment_instruments.csv
    payment_payload = get_valid_payment_payload(instrument_id="instr_fail")

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      accepted_codes={"payment_failed"},
      error_4xx_substring="payment",
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_complete_without_fulfillment(self) -> None:
    """Test completion rejection when fulfillment is missing.

    Given a newly created checkout session without fulfillment details,
    When a completion request is submitted,
    Then the server either rejects with a 4xx describing the fulfillment
    problem, or returns an in-band error message with code 'missing' or
    'field_required'.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_id = response_json["id"]

    payment_payload = get_valid_payment_payload()

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      accepted_codes={"missing", "field_required"},
      error_4xx_substring="fulfillment",
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_structured_error_messages(self) -> None:
    """Test that error responses carry structured, machine-readable detail.

    Given a request that triggers an error (e.g., out of stock),
    Then a 4xx rejection must carry a structured 'detail' field describing
    the error, and an in-band answer must carry the full message envelope
    (type, code, content, severity) — the structural requirement behind
    both postures.
    """
    # Get out of stock item from config
    out_of_stock_item = self.conformance_config.get(
      "out_of_stock_item",
      {"id": "out_of_stock_item_1", "title": "Out of Stock Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=out_of_stock_item["id"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      accepted_codes={"out_of_stock", "item_unavailable"},
      error_4xx_substring="stock",
    )

  @spec_assert("REQ-SHOPPING-CHECKOUT-0044")
  def test_message_paths_are_rfc9535_jsonpath(self) -> None:
    """Every `messages[].path` must be an RFC 9535 JSONPath relative to root."""
    created = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**created)
    qty_resp = self._request_line_quantity(
      checkout_obj, self._EXCESSIVE_QUANTITY
    )
    out_of_stock_item = self.conformance_config.get(
      "out_of_stock_item",
      {"id": "out_of_stock_item_1", "title": "Out of Stock Item"},
    )
    oos_payload = self.create_checkout_payload(item_id=out_of_stock_item["id"])
    oos_resp = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=oos_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )

    samples: list[tuple[str, dict]] = [("create", created)]
    for label, resp in (
      ("excessive_quantity", qty_resp),
      ("out_of_stock", oos_resp),
    ):
      try:
        parsed = resp.json()
      except ValueError:
        continue
      if isinstance(parsed, dict):
        samples.append((label, parsed))

    checked = 0
    for label, body in samples:
      messages = body.get("messages")
      if not isinstance(messages, list):
        continue
      for idx, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("path") is not None:
          checked += 1
          self.assert_rfc9535_jsonpath(
            msg["path"],
            body,
            where=f"{label}.messages[{idx}].path",
          )

    if checked == 0:
      self.skip_not_exercised(
        "server returned no messages[] entry with a 'path' field across "
        "create, out-of-stock, and excessive-quantity responses"
      )

  @spec_assert("REQ-SHOPPING-CHECKOUT-0083")
  def test_disclosure_item_emits_disclosure_warning(self) -> None:
    """Items requiring component disclosures set `presentation: disclosure`."""
    disclosure_sku = self.require_trigger("disclosure_item_sku")
    body = self.create_checkout_session(
      item_id=disclosure_sku, select_fulfillment=False
    )
    self.assert_disclosure_warning(
      body,
      require_policy_link=False,
      where="POST /checkout-sessions",
    )

  @spec_assert("REQ-SHOPPING-CHECKOUT-0086")
  def test_policy_disclosure_code_matches_policy_type(self) -> None:
    """Policy disclosures set `code` equal to the structured `policy.type`."""
    disclosure_sku = self.require_trigger("disclosure_item_sku")
    body = self.create_checkout_session(
      item_id=disclosure_sku, select_fulfillment=False
    )
    self.assert_disclosure_warning(
      body,
      require_policy_link=True,
      where="POST /checkout-sessions",
    )


if __name__ == "__main__":
  absltest.main()

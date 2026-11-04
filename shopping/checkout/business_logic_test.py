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

"""Business logic and totals tests for the UCP Shopping vertical."""

from absl.testing import absltest
from framework.decorators import (
  no_rule,
  older_version,
  requires_capability,
  spec_assert,
)
from shopping.base import (
  build_consent_confirmation,
  quantity_unit_machine_identity,
  ShoppingIntegrationTestBase,
)
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import (
  checkout_update_request as checkout_update_req,
)

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
from ucp_sdk.models.schemas.shopping.types import buyer_update_request
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


@requires_capability("dev.ucp.shopping.checkout")
class BusinessLogicTest(ShoppingIntegrationTestBase):
  """Tests for business logic and calculations.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - GET /checkout-sessions/{id}
  """

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0016",  # line total MUST be price x quantity
    "REQ-SHOPPING-CHECKOUT-0134",  # Exactly one "total" MUST be present
  )
  def test_totals_calculation_on_create(self):
    """Test that totals are calculated correctly upon checkout creation.

    Given a request to create a checkout session for three of an item,
    When the checkout is created,
    Then every line's total is the server's own `price × quantity ×
    10^-scale` (checkout.md, Pricing a line), the first line prices the item
    at the fixtures' price, and the checkout totals reconcile.
    """
    expected_price = self.fixture_ctx.get_test_price()

    # Create checkout (client cannot send title/price per schema). The server
    # should use the authoritative price from its DB (which matches our config).
    response_json = self.create_checkout_session(
      quantity=3, select_fulfillment=False
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Each line's total follows from its own price, quantity and scale. A
    # measured line (scale > 0) is checked with the rule's single rounding;
    # the flower shop sells whole items, so scale is 0 here.
    self.assert_line_totals(response_json)

    # The store's data: the item costs what the fixtures say, and the line
    # total is three of it.
    line_item = checkout_obj.line_items[0]
    self.assertEqual(line_item.quantity, 3)
    self.assertEqual(
      line_item.item.price,
      expected_price,
      f"Item price should match DB price {expected_price}",
    )
    li_total = next(
      (t.amount for t in line_item.totals if t.type == "total"), None
    )
    self.assertEqual(
      li_total,
      expected_price * 3,
      f"Line item total should be 3 x DB price {expected_price}",
    )

    # Verify Totals Breakdown
    self.assert_totals_consistent(checkout_obj, expected_price * 3)

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0016",  # line total MUST be price x quantity
    "REQ-SHOPPING-CHECKOUT-0134",  # Exactly one "total" MUST be present
  )
  def test_totals_recalculation_on_update(self):
    """Test that totals are recalculated correctly upon checkout update.

    Given an existing checkout session with 1 item,
    When the line item quantity is updated to 2 (PUT replaces the whole
    checkout, so every other field is resent as it was),
    Then the line's own total is `price × 2`, and the checkout totals are
    2 * price.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    expected_price = self.fixture_ctx.get_test_price()

    # Update quantity to 2.
    line_item_update = line_item_update_request.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update_request.ItemUpdateRequest(
        id=checkout_obj.line_items[0].item.id,
      ),
      quantity=2,
    )
    updated_json = self.update_checkout_session(
      checkout_obj, line_items=[line_item_update]
    )
    self.assert_line_totals(updated_json)

    updated_checkout = checkout.Checkout(**updated_json)
    self.assertEqual(updated_checkout.line_items[0].quantity, 2)
    self.assert_totals_consistent(updated_checkout, expected_price * 2)

  @requires_capability("dev.ucp.shopping.buyer_consent")
  @older_version(
    "2026-04-08",
    "sends boolean consent flags (marketing, analytics); the draft keys "
    "purposes by reverse-DNS ID and businesses MUST ignore unadvertised "
    "ones (REQ-SHOPPING-BUYER-CONSENT-0010)",
  )
  def test_buyer_consent(self):
    """Test that buyer consent preferences are persisted on creation.

    Given a checkout creation payload including buyer consent preferences
    (marketing=True, analytics=False),
    When the checkout session is created,
    Then the returned checkout object should correctly reflect these consent
    values.
    """
    create_payload = self.create_checkout_payload()

    # Consent is sent as a plain mapping: in newer SDK releases
    # `buyer_consent.Consent` is a `TypeAliasType`
    # (dict[ReverseDomainName, ConsentPurpose]) and cannot be instantiated
    # with keyword arguments, so a dict keeps this test working across
    # SDK versions.
    consent_dict = {
      "marketing": True,
      "analytics": False,
      "sale_of_data": False,
    }

    create_payload_dict = create_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    create_payload_dict["buyer"] = {
      "first_name": "Consent",
      "last_name": "Tester",
      "email": "consent@example.com",
      "consent": consent_dict,
    }

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload_dict,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, [200, 201])
    checkout_id = checkout.Checkout(**response.json()).id

    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    checkout_obj = checkout.Checkout(**response.json())
    self.assertTrue(checkout_obj.buyer, "Buyer info missing")

    # buyer is types.buyer.Buyer, consent is in extra fields
    consent_data = getattr(checkout_obj.buyer, "consent", None)
    self.assertTrue(consent_data, "Consent info missing")

    if isinstance(consent_data, dict):
      self.assertTrue(
        consent_data.get("marketing"),
        f"Marketing consent not persisted. Resp: {consent_data}",
      )
      self.assertFalse(
        consent_data.get("analytics"),
        f"Analytics consent not persisted. Resp: {consent_data}",
      )
    else:
      self.assertTrue(
        getattr(consent_data, "marketing", False),
        f"Marketing consent not persisted. Resp: {consent_data}",
      )
      self.assertFalse(
        getattr(consent_data, "analytics", True),
        f"Analytics consent not persisted. Resp: {consent_data}",
      )

  @no_rule(
    "checkout/index.md, Update Checkout: the request 'will replace the "
    "existing checkout session state on the business side', with no "
    "keyword"
  )
  def test_buyer_info_persistence(self):
    """Test that buyer information is persisted on update.

    Given an existing checkout session,
    When the session is updated with new buyer details (email, name),
    Then the retrieved checkout session should reflect these updated buyer
    details.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Update with buyer info
    item_update = item_update_request.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
    )
    line_item_update = line_item_update_request.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=1,
    )
    payment_update = payment_update_request.PaymentUpdateRequest(
      instruments=checkout_obj.payment.instruments,
    )

    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
      buyer=buyer_update_request.BuyerUpdateRequest(
        email="test@example.com",
        first_name="Test",
        last_name="User",
      ),
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    # GET and verify
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    checkout_obj = checkout.Checkout(**response.json())
    self.assertTrue(checkout_obj.buyer, "Buyer info missing")
    self.assertEqual(
      checkout_obj.buyer.email, "test@example.com", "Email mismatch"
    )
    self.assertEqual(
      checkout_obj.buyer.first_name, "Test", "First name mismatch"
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0003",  # compare quantity_unit machine identities
    "REQ-SHOPPING-CHECKOUT-0005",  # reject unreconcilable unit with error
  )
  def test_quantity_unit_machine_identity_comparison_and_mismatch_rejection(
    self,
  ) -> None:
    """Business compares `quantity_unit` identities and rejects mismatches."""
    sku = (
      self.fixture_ctx.get_measured_item_sku()
      or self.fixture_ctx.get_test_sku()
    )
    created = self.create_checkout_session(
      item_id=sku, quantity=1, select_fulfillment=False
    )
    checkout_id = created["id"]
    lines = created.get("line_items") or []
    self.assertTrue(lines, f"checkout has no line_items: {created}")
    before_line = lines[0]
    auth_unit, auth_scale = quantity_unit_machine_identity(
      (before_line.get("item") or {}).get("quantity_unit")
    ) or ("C62", 0)

    # 1. Explicit assertion with matching (unit, scale) and alternate
    #    display_text MUST match and apply the updated quantity.
    matching_payload = self.checkout_update_payload(
      checkout.Checkout(**created)
    )
    matching_payload["line_items"][0]["quantity"] = 2
    matching_payload["line_items"][0]["item"]["quantity_unit"] = {
      "unit": auth_unit,
      "scale": auth_scale,
      "display_text": "alt-unit-label",
    }
    matching_resp = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=matching_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(matching_resp, 200)
    matching_checkout = matching_resp.json()

    # 2. Assertion with an incompatible unit identity MUST be rejected with a
    #    recoverable error and leave the line at its previous state.
    mismatch_unit = "HUR" if auth_unit != "HUR" else "LTR"
    mismatch_payload = self.checkout_update_payload(
      checkout.Checkout(**matching_checkout)
    )
    mismatch_payload["line_items"][0]["quantity"] = 3
    mismatch_payload["line_items"][0]["item"]["quantity_unit"] = {
      "unit": mismatch_unit,
      "scale": 1,
      "display_text": "hr" if mismatch_unit == "HUR" else "0.1 L",
    }
    mismatch_resp = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=mismatch_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(mismatch_resp, 200)
    mismatch_checkout = mismatch_resp.json()

    self.assert_quantity_unit_comparison(
      before_line,
      matching_checkout,
      mismatch_checkout,
      expected_matching_qty=2,
      where=f"PUT /checkout-sessions/{checkout_id}",
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0015",  # include quantity_unit when != ('C62', 0)
  )
  def test_measured_item_echoes_non_default_quantity_unit(self) -> None:
    """Line includes `item.quantity_unit` when sale basis != ('C62', 0)."""
    sku = self.require_trigger("measured_item_sku")
    created = self.create_checkout_session(
      item_id=sku, select_fulfillment=False
    )
    lines = created.get("line_items") or []
    self.assertTrue(lines, f"checkout has no line_items: {created}")
    self.assert_non_default_quantity_unit(
      lines[0], where=f"checkout line_items[0] ({sku!r})"
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0019",  # include unit_price when pricing != sale
  )
  def test_line_item_includes_unit_price_when_pricing_basis_differs(
    self,
  ) -> None:
    """Line includes `item.unit_price` when pricing basis != sale basis."""
    sku = self.require_trigger("unit_price_item_sku")
    created = self.create_checkout_session(
      item_id=sku, select_fulfillment=False
    )
    lines = created.get("line_items") or []
    self.assertTrue(lines, f"checkout has no line_items: {created}")
    self.assert_line_pricing_basis_unit_price(
      lines[0],
      created.get("currency"),
      where=f"checkout line_items[0] ({sku!r})",
    )

  @requires_capability("dev.ucp.shopping.buyer_consent")
  @spec_assert(
    "REQ-SHOPPING-BUYER-CONSENT-0010",  # ignore unadvertised purposes/segments
  )
  def test_unadvertised_consent_purposes_and_segments_ignored(self) -> None:
    """Business ignores unadvertised consent purposes and segments."""
    buyer_base = {
      "first_name": "Consent",
      "last_name": "Tester",
      "email": "consent@example.com",
    }
    created = self.create_checkout_session(
      buyer=buyer_base, select_fulfillment=False
    )
    checkout_id = created["id"]
    checkout_obj = checkout.Checkout(**created)
    advertised = (created.get("buyer") or {}).get("consent")
    confirm_consent = build_consent_confirmation(
      advertised,
      unadvertised_purpose="org.ucp.conformance.unadvertised_purpose",
      unadvertised_segment="org.ucp.conformance.unadvertised_segment",
    )
    updated = self.update_checkout_session(
      checkout_obj,
      buyer={**buyer_base, "consent": confirm_consent},
    )
    self.assert_unadvertised_consent_ignored(
      created,
      updated,
      where=f"PUT /checkout-sessions/{checkout_id}",
    )

  @requires_capability("dev.ucp.shopping.buyer_consent")
  @spec_assert(
    "REQ-SHOPPING-BUYER-CONSENT-0003",  # must not complete with unmet dep
    "REQ-SHOPPING-BUYER-CONSENT-0004",  # surface missing dep via error flow
  )
  def test_consent_unmet_dependency_blocks_completion_with_error(self) -> None:
    """Unmet consent data dependency blocks completion and surfaces error."""
    dep = self.require_trigger("consent_with_dependency")
    buyer_base = {
      "first_name": "Consent",
      "last_name": "Tester",
      "email": "consent@example.com",
    }
    created = self.create_checkout_session(buyer=buyer_base)
    checkout_id = created["id"]
    advertised = (created.get("buyer") or {}).get("consent")
    confirm_consent = build_consent_confirmation(
      advertised,
      grant_purpose=dep["purpose"],
      grant_segment=dep.get("segment"),
    )
    buyer_payload = {**buyer_base, "consent": confirm_consent}
    buyer_payload.pop(dep["missing_buyer_field"], None)

    checkout_obj = checkout.Checkout(**created)
    self.update_checkout_session(checkout_obj, buyer=buyer_payload)

    complete_resp = self.attempt_complete_checkout(
      checkout_id, buyer=buyer_payload
    )
    get_resp = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    after_checkout = get_resp.json() if get_resp.status_code == 200 else None
    self.assert_consent_unmet_dependency_completion(
      complete_resp,
      after_checkout=after_checkout,
      where=f"POST /checkout-sessions/{checkout_id}/complete ({dep!r})",
    )


if __name__ == "__main__":
  absltest.main()

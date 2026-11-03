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

"""Conformance tests for the cart capability (cart.md).

Adopted from conformance PR #108, whose four assertions this module keeps,
moved into ``shopping/cart/`` with the suite's markers and helpers and
tightened where a rule asks for more than the original checked:

  * CART-0003 and CART-0004: on conversion the business MUST use the cart
    contents and MUST ignore overlapping fields in the checkout payload.
    Checked for the line items and for the buyer, two of the three cart
    contents the page says initialize the checkout.
  * CART-0005: a second conversion of a cart whose checkout is still
    incomplete MUST return that checkout.
  * CART-0025: cancel MUST return the cart state before deletion. Compared
    with Get Cart taken just before, on the state a cart carries: lines,
    currency, totals, buyer, context and discounts.
  * DISCOUNT-0003: codes applied to a cart MUST carry forward to the
    checkout. The checkout has to name the code: in ``discounts.codes``,
    as an applied discount, or in a ``messages[]`` entry rejecting it (the
    SHOULD that follows the rule). A lower total alone does not show the
    code travelled, so it no longer counts.

Not asserted: CART-0001 and CART-0024 bind the platform, and "subsequent
operations return not_found" after cancel is a SHOULD.
"""

from typing import Any

from absl.testing import absltest
from framework.decorators import requires_capability, spec_assert
from framework.outcomes import CAPABILITY, skip_reason
from shopping.base import ShoppingIntegrationTestBase, snapshot_differences

_CART = "dev.ucp.shopping.cart"
_CHECKOUT = "dev.ucp.shopping.checkout"
_DISCOUNT = "dev.ucp.shopping.discount"

_CART_BUYER = {
  "first_name": "Cart",
  "last_name": "Buyer",
  "email": "cart-buyer@example.com",
}
_PAYLOAD_BUYER = {
  "first_name": "Payload",
  "last_name": "Buyer",
  "email": "payload-buyer@example.com",
}

# The members that make up a cart's state (cart.json), as opposed to the
# envelope and bookkeeping a response may refresh (ucp, messages, links,
# continue_url, expires_at).
_CART_STATE = (
  "line_items",
  "currency",
  "totals",
  "buyer",
  "context",
  "discounts",
)


def _cart_state(cart: dict[str, Any]) -> dict[str, Any]:
  """Return the members of a cart that describe its state."""
  return {key: cart.get(key) for key in _CART_STATE}


def _names_code(entity: dict[str, Any], code: str) -> bool:
  """Whether a cart or checkout lists the code, submitted or applied."""
  discounts = entity.get("discounts") or {}
  wanted = code.casefold()
  if wanted in [str(c).casefold() for c in discounts.get("codes") or []]:
    return True
  return any(
    str(applied.get("code") or "").casefold() == wanted
    for applied in discounts.get("applied") or []
    if isinstance(applied, dict)
  )


def _message_names_code(entity: dict[str, Any], code: str) -> bool:
  """Whether a messages[] entry mentions the code, as a rejection would."""
  wanted = code.casefold()
  for message in entity.get("messages") or []:
    if not isinstance(message, dict):
      continue
    text = " ".join(
      str(message.get(key) or "") for key in ("code", "content", "path")
    )
    if wanted in text.casefold():
      return True
  return False


@requires_capability(_CART)
class CartTest(ShoppingIntegrationTestBase):
  """Cart capability conformance (cart.md).

  Validated Paths:
  - POST /carts
  - GET /carts/{id}
  - POST /carts/{id}/cancel
  - POST /checkout-sessions with cart_id
  """

  # ── cart.md, Cart-to-Checkout Conversion ────────────────────────────────
  @spec_assert(
    "REQ-SHOPPING-CART-0003",  # the business MUST use the cart contents
    "REQ-SHOPPING-CART-0004",  # MUST ignore overlapping fields in the payload
  )
  def test_conversion_uses_cart_line_items_over_payload(self) -> None:
    """The checkout's line items are the cart's, not the payload's.

    Given a cart holding two of the item,
    When Create Checkout is called with its cart_id and a line_items array
    asking for nine,
    Then the checkout holds the cart's lines (CART-0003), not the
    payload's (CART-0004).
    """
    cart = self.create_cart(quantity=2)
    conflicting = [
      {"item": {"id": self.fixture_ctx.get_test_sku()}, "quantity": 9}
    ]
    checkout = self.convert_cart(cart["id"], line_items=conflicting)
    self.assertEqual(
      self.line_item_bag(checkout),
      self.line_item_bag(cart),
      "the checkout's line items must come from the cart (cart.md: the "
      "business MUST use the cart contents and MUST ignore overlapping "
      f"fields in the checkout payload); the payload asked for {conflicting}",
    )

  @spec_assert(
    "REQ-SHOPPING-CART-0003",  # the business MUST use the cart contents
    "REQ-SHOPPING-CART-0004",  # MUST ignore overlapping fields in the payload
  )
  def test_conversion_uses_cart_buyer_over_payload(self) -> None:
    """The checkout's buyer is the cart's, not the payload's.

    cart.md: the cart contents (line_items, context, buyer) initialize the
    checkout session. Given a cart created with a buyer, when the
    conversion request names a different buyer, the checkout carries the
    cart's. A server whose cart does not keep the buyer it was created
    with leaves nothing to initialize the checkout with, so the test skips
    as not exercised (no rule makes a cart keep its buyer).
    """
    cart = self.create_cart(buyer=_CART_BUYER)
    if (cart.get("buyer") or {}).get("email") != _CART_BUYER["email"]:
      self.skip_not_exercised(
        "the cart does not carry the buyer it was created with, so there "
        "is no cart buyer to initialize the checkout with: "
        f"{cart.get('buyer')!r}"
      )
    checkout = self.convert_cart(cart["id"], buyer=_PAYLOAD_BUYER)
    self.assertEqual(
      (checkout.get("buyer") or {}).get("email"),
      _CART_BUYER["email"],
      "the checkout's buyer must be the cart's (cart.md: the cart contents, "
      "buyer included, initialize the checkout; overlapping payload fields "
      f"are ignored); got {checkout.get('buyer')!r}",
    )

  @spec_assert(
    "REQ-SHOPPING-CART-0005",  # MUST return the existing incomplete checkout
  )
  def test_second_conversion_returns_the_existing_checkout(self) -> None:
    """Converting a cart twice yields the same incomplete checkout.

    Given a cart converted once, while that checkout is still incomplete,
    When Create Checkout is called again with the same cart_id (a new
    request with its own Idempotency-Key, so not a replay),
    Then the response is the existing checkout session, not a new one.
    """
    cart = self.create_cart()
    first = self.convert_cart(cart["id"])
    self.assertNotIn(
      first.get("status"),
      ("completed", "canceled"),
      "precondition: the first conversion must leave an incomplete checkout",
    )
    second = self.convert_cart(cart["id"])
    self.assertEqual(
      second["id"],
      first["id"],
      "a second conversion must return the existing checkout (cart.md: if an "
      "incomplete checkout already exists for the cart_id, the business MUST "
      "return it rather than creating a new one)",
    )

  # ── cart.md, Operations: Cancel Cart ─────────────────────────────────────
  @spec_assert(
    "REQ-SHOPPING-CART-0025",  # MUST return the cart state before deletion
  )
  def test_cancel_returns_the_cart_state_before_deletion(self) -> None:
    """Cancel answers with the cart as it was before deletion.

    Given a cart, read back with Get Cart just before,
    When it is canceled,
    Then the response is that cart: the same id, and the same lines,
    currency, totals, buyer, context and discounts.
    """
    cart = self.create_cart(quantity=3, buyer=_CART_BUYER)
    before_response = self.get_cart(cart["id"])
    self.assert_response_status(before_response, 200)
    before = before_response.json()

    response = self.cancel_cart(cart["id"])
    self.assert_response_status(response, [200, 201])
    canceled = response.json()
    self.assertEqual(
      canceled.get("id"),
      cart["id"],
      "cancel must return the canceled cart, identified by its own id",
    )
    differences = snapshot_differences(
      _cart_state(before), _cart_state(canceled), reference="Get Cart"
    )
    self.assertFalse(
      differences,
      "cancel must return the cart state before deletion (cart.md Cancel "
      "Cart); differences from the cart read just before:\n  "
      + "\n  ".join(differences),
    )

  # ── discount.md, Operations: Cart-to-checkout continuity ────────────────
  @requires_capability(_DISCOUNT)
  @spec_assert(
    "REQ-SHOPPING-DISCOUNT-0003",  # MUST carry forward codes applied to a cart
  )
  def test_cart_discount_codes_carry_forward_to_checkout(self) -> None:
    """A code applied to a cart is on the checkout it converts into.

    discount.md: when a cart is converted via cart_id, businesses MUST
    carry forward any discount codes applied to the cart; codes no longer
    valid at checkout time SHOULD be reported in messages[]. So the
    checkout must name the code: in discounts.codes, as an applied
    discount, or in a message rejecting it.

    Needs the discount extension advertised for both cart and checkout
    (discount.md: a business MAY extend either or both); otherwise the
    test skips as a capability gap. It skips as not exercised when the
    server does not apply the fixtures' code to the cart in the first
    place, since then no applied code could carry forward.
    """
    profile = self.server_profile
    for capability in (_CART, _CHECKOUT):
      if profile is not None and not profile.extension_extends(
        _DISCOUNT, capability
      ):
        self.skipTest(
          skip_reason(CAPABILITY, f"{_DISCOUNT} extending {capability}")
        )
    code = self.fixture_ctx.get_test_discount_code()
    cart = self.create_cart(discounts={"codes": [code]})
    if not _names_code(cart, code):
      self.skip_not_exercised(
        f"the server did not apply discount code {code!r} to the cart (not "
        "in discounts.codes or discounts.applied), so no applied code could "
        f"carry forward: {cart.get('discounts')!r}"
      )

    checkout = self.convert_cart(cart["id"])
    self.assertTrue(
      _names_code(checkout, code) or _message_names_code(checkout, code),
      f"discount code {code!r} applied to the cart is absent from the "
      "checkout it converted into: not in discounts.codes, not applied, and "
      "no messages[] entry rejects it (discount.md: businesses MUST carry "
      "forward codes applied to the cart); checkout discounts: "
      f"{checkout.get('discounts')!r}, messages: {checkout.get('messages')!r}",
    )


if __name__ == "__main__":
  absltest.main()

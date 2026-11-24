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

"""Conformance tests for the discount capability (discount.md).

Complements discount_test.py (discount flow, multiple-code accept/reject,
fixed-amount) by covering discount MUSTs the existing suite does not assert:

  DSC-003  a new codes[] set replaces the previous one
  DSC-004  an empty codes[] removes all discounts
  DSC-005  codes are matched case-insensitively
  DSC-021  a discount's totals[] entry is negative (total.json exclusiveMax 0)
  DSC-022  an applied discount's allocations[] sum to its amount
  DSC-027  client-supplied applied[] (response-only) must not change pricing

Server-agnostic: assertions are relative (a discount reduced the total; the
applied code is echoed; the discount total is negative; an empty code set
removes it; allocations reconcile) rather than tied to a specific discount
value, so any conformant business passes regardless of the discount amount.
A discount total is a `discount` or an `items_discount` entry of totals[]:
discount.md reports a discount allocated to line items as the latter. And
what a code did is read against the discount totals the checkout carried
before it, since a business may discount a checkout on its own account.
Configure the codes via conformance_input.json under test_fixtures
(valid_discount_code / valid_discount_code_2); the tests skip honestly when no
code is supplied or when the business does not advertise the capability.
"""

from absl.testing import absltest
from framework.decorators import (
  no_rule,
  requires_capability,
  spec_assert,
)
from shopping.base import (
  ShoppingIntegrationTestBase,
  discount_amounts,
  discount_total_entries,
)
from ucp_sdk.models.schemas.shopping import checkout as checkout


@requires_capability("dev.ucp.shopping.discount")
class DiscountSemanticsTest(ShoppingIntegrationTestBase):
  """Discount capability conformance (discount.md)."""

  def setUp(self) -> None:
    """Resolve the discount code the business declared for the suite."""
    super().setUp()
    self._code = self.fixture_ctx.get_test_discount_code()

  # ── shared drivers (operate on the raw response dict, as discount.md
  #    describes the wire shape and the sample suite asserts) ──────────────
  def _new_checkout(self):
    """Create a checkout and return (checkout_model, raw_dict)."""
    raw = self.create_checkout_session(select_fulfillment=False)
    return checkout.Checkout(**raw), raw

  def _apply_codes(self, checkout_obj, codes):
    """Apply a discount code set; return the raw updated-checkout dict."""
    return self.update_checkout_session(
      checkout_obj,
      discounts={"codes": codes},
      headers=self.get_headers(),
    )

  @staticmethod
  def _discounts(raw):
    """Return the discount totals of a response, as (type, amount) pairs.

    Both `discount` and `items_discount` entries: a discount allocated to
    line items is reported as the second (discount.md, Impact on Line Items
    and Totals).
    """
    return discount_amounts(raw.get("totals"))

  @staticmethod
  def _total_amount(raw):
    t = next(
      (t for t in (raw.get("totals") or []) if t.get("type") == "total"), None
    )
    return t.get("amount") if t else None

  @staticmethod
  def _applied_codes(raw):
    applied = (raw.get("discounts") or {}).get("applied") or []
    return [
      a.get("code") for a in applied if isinstance(a, dict) and a.get("code")
    ]

  # ── DSC-005: case-insensitive matching (discount.md) ────────────────────
  @no_rule(
    "discount.md, Operations: 'Codes are matched case-insensitively by "
    "business', with no keyword"
  )
  def test_code_matches_case_insensitively(self):
    """A code submitted in a different case still matches and applies."""
    base, base_raw = self._new_checkout()
    swapped = self._code.swapcase()
    self.assertNotEqual(
      swapped, self._code, "test code has no letters to change case on"
    )
    raw = self._apply_codes(base, [swapped])
    applied = [c.upper() for c in self._applied_codes(raw)]
    self.assertIn(
      self._code.upper(),
      applied,
      f"case-variant '{swapped}' of '{self._code}' should match and appear in "
      f"discounts.applied (got {applied})",
    )
    self.assertNotEqual(
      self._discounts(raw),
      self._discounts(base_raw),
      "a matched discount must produce a discount total: a discount or "
      "items_discount entry in totals[] that was not there before the code "
      f"(totals: {raw.get('totals')})",
    )

  # ── discount total is negative (discount.md / total.json) ───────────────
  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0142",  # subtractive types MUST be negative
  )
  def test_discount_total_is_negative(self):
    """A discount's totals[] entry is negative (total.json exclusiveMax 0).

    Every `discount` and `items_discount` entry of the discounted checkout
    is checked, and applying a valid code must have added one.
    """
    base, base_raw = self._new_checkout()
    total_before = self._total_amount(base_raw)
    raw = self._apply_codes(base, [self._code])
    self.assertNotEqual(
      self._discounts(raw),
      self._discounts(base_raw),
      "applying a valid code must add a discount total: a discount or "
      f"items_discount entry in totals[] (totals: {raw.get('totals')})",
    )
    for entry in discount_total_entries(raw.get("totals")):
      amount = entry.get("amount")
      self.assertIsInstance(
        amount, int, f"a discount totals[] entry needs an amount: {entry}"
      )
      self.assertLess(
        amount,
        0,
        f"a {entry['type']} totals[] entry must be negative, got {amount}",
      )
    if total_before is not None:
      self.assertLess(
        self._total_amount(raw),
        total_before,
        "the order total must decrease when a discount applies",
      )

  # ── DSC-022: allocations sum to the applied discount amount ─────────────
  @no_rule(
    "discount.md, Allocations Array: an 'Invariant' that allocations sum to "
    "applied_discount.amount, with no keyword"
  )
  def test_allocations_sum_to_applied_amount(self):
    """Each applied discount's allocations sum to its amount (discount.md).

    A cross-field invariant the JSON schema cannot express: when a discount
    carries allocations[], their amounts must sum to applied_discount.amount.
    Skips honestly for a server that does not emit allocations (they are
    optional per the schema).
    """
    base, _ = self._new_checkout()
    raw = self._apply_codes(base, [self._code])
    applied = (raw.get("discounts") or {}).get("applied") or []
    self.assertTrue(applied, "a valid code must produce an applied discount")
    checked = 0
    for a in applied:
      allocs = a.get("allocations") or []
      if not allocs:
        continue
      checked += 1
      self.assertEqual(
        sum(al.get("amount", 0) for al in allocs),
        a.get("amount"),
        f"allocations must sum to applied_discount.amount for "
        f"'{a.get('code')}' (got {[al.get('amount') for al in allocs]} vs "
        f"{a.get('amount')})",
      )
    if checked == 0:
      self.skip_not_exercised("the server emits no discount allocations[]")

  # ── DSC-027: client-supplied applied[] must not change pricing ──────────
  @no_rule(
    "discount.md, Operations: codes fall under 'Request behavior' and "
    "applied under 'Response behavior'; no keyword says a submitted applied[] "
    "is ignored"
  )
  def test_client_applied_does_not_change_price(self):
    """Injected discounts.applied[] must not lower the priced total.

    discounts.applied is response-only (discount.json marks it
    ucp_request: omit). A server prices discounts from codes[], so a client
    that injects a large applied[] amount must not obtain a cheaper order.
    Asserts the total equals applying the same code without the injection,
    rather than asserting the server rejects the field (the spec constrains
    the request sender, not the server's leniency).
    """
    base_a, _ = self._new_checkout()
    clean = self._apply_codes(base_a, [self._code])
    clean_total = self._total_amount(clean)
    self.assertIsNotNone(clean_total, "need a computed total to compare")
    base_b, _ = self._new_checkout()
    injected = self.update_checkout_session(
      base_b,
      discounts={
        "codes": [self._code],
        "applied": [
          {
            "code": "__INJECT__",
            "title": "injected",
            "amount": 10_000_000,
            "allocations": [{"path": "subtotal", "amount": 10_000_000}],
          }
        ],
      },
      headers=self.get_headers(),
    )
    self.assertEqual(
      self._total_amount(injected),
      clean_total,
      "client-supplied discounts.applied must not change the priced total "
      "(applied is response-only; the server prices from codes[])",
    )

  # ── DSC-004: empty codes[] removes all discounts ────────────────────────
  @no_rule(
    "discount.md, Operations: an empty codes array removes all discount "
    "codes, with no keyword"
  )
  def test_empty_codes_removes_discount(self):
    """Sending codes:[] removes previously applied discounts."""
    base, base_raw = self._new_checkout()
    applied_raw = self._apply_codes(base, [self._code])
    self.assertNotEqual(
      self._discounts(applied_raw),
      self._discounts(base_raw),
      "precondition: code applied",
    )
    cleared = self._apply_codes(checkout.Checkout(**applied_raw), [])
    self.assertEqual(
      self._discounts(cleared),
      self._discounts(base_raw),
      "codes:[] must remove the discount: the discount and items_discount "
      "entries of totals[] should be what they were before the code",
    )
    self.assertEqual(
      self._applied_codes(cleared), [], "applied[] must be empty after clearing"
    )

  # ── DSC-003: a new codes[] set replaces the previous one ────────────────
  @no_rule(
    "discount.md, Operations: 'Submitting discounts.codes replaces any "
    "previously submitted codes', with no keyword"
  )
  def test_codes_replace_previous_set(self):
    """Submitting discounts.codes replaces any previously submitted codes."""
    second = self.require_trigger("valid_discount_code_2")
    base, _ = self._new_checkout()
    applied_raw = self._apply_codes(base, [self._code])
    applied_obj = checkout.Checkout(**applied_raw)
    replaced = self._apply_codes(applied_obj, [second])
    applied = [c.upper() for c in self._applied_codes(replaced)]
    self.assertIn(
      second.upper(), applied, "the replacement code must be applied"
    )
    self.assertNotIn(
      self._code.upper(),
      applied,
      f"the first code must be replaced, not accumulated (got {applied})",
    )

  # ── W7: provisional discounts from eligibility claims (discount.md) ─────
  @spec_assert("REQ-SHOPPING-DISCOUNT-0006", "REQ-SHOPPING-DISCOUNT-0008")
  def test_pricing_eligibility_claim_surfaces_provisional_discount(self):
    """Accepted pricing eligibility claims surface provisional discounts."""
    claim = self.require_trigger("pricing_eligibility_claim")
    _, baseline_raw = self._new_checkout()
    baseline_total = self._total_amount(baseline_raw)
    self.assertIsNotNone(baseline_total, "baseline checkout must have a total")
    claimed_raw = self.create_checkout_session(
      select_fulfillment=False,
      context={"eligibility": [claim]},
    )
    self.assert_provisional_discount(
      claimed_raw, claim, where="discounts.applied"
    )
    claimed_total = self._total_amount(claimed_raw)
    self.assertIsNotNone(claimed_total, "claimed checkout must have a total")
    self.assertLess(
      claimed_total,
      baseline_total,
      f"provisional discount for {claim!r} must reduce total below "
      f"{baseline_total}, got {claimed_total}",
    )

  @spec_assert("REQ-SHOPPING-DISCOUNT-0009")
  def test_provisional_discount_claims_resolved_at_completion(self):
    """Provisional discount claims must be resolved at checkout completion."""
    claim = self.require_trigger("pricing_eligibility_claim")
    created = self.create_checkout_session(
      buyer=self.DEFAULT_BUYER,
      select_fulfillment=True,
      context={"eligibility": [claim]},
    )
    self.assert_provisional_discount(
      created, claim, where="created checkout discounts.applied"
    )
    resp = self.attempt_complete_checkout(created["id"])
    try:
      body = resp.json()
    except ValueError:
      self.fail(
        f"complete response for provisional claim {claim!r} was not JSON "
        f"(HTTP {resp.status_code})"
      )
    if resp.status_code in (200, 201) and body.get("status") == "completed":
      applied = (body.get("discounts") or {}).get("applied") or []
      still_provisional = [
        d
        for d in applied
        if isinstance(d, dict) and d.get("provisional") is True
      ]
      self.assertEmpty(
        still_provisional,
        msg=(
          f"completed checkout must have resolved all provisional discounts "
          f"(provisional=false or removed), got {still_provisional}"
        ),
      )
    else:
      self.assert_eligibility_invalid_error(body, where="complete response")

    rescind_checkout = self.create_checkout_session(
      buyer=self.DEFAULT_BUYER,
      select_fulfillment=True,
      context={"eligibility": [claim]},
    )
    self.assert_provisional_discount(
      rescind_checkout, claim, where="pre-rescission discounts.applied"
    )
    updated = self.update_checkout_session(
      checkout.Checkout(**rescind_checkout),
      context={"eligibility": []},
    )
    remaining = [
      d
      for d in ((updated.get("discounts") or {}).get("applied") or [])
      if isinstance(d, dict)
      and (d.get("provisional") is True or d.get("eligibility") == claim)
    ]
    self.assertEmpty(
      remaining,
      msg=(
        f"rescinding context.eligibility=[] must remove provisional discount "
        f"for {claim!r}, got {remaining}"
      ),
    )
    completed = self.complete_checkout_session(rescind_checkout["id"])
    self.assertEqual(completed.get("status"), "completed")
    completed_provisional = [
      d
      for d in ((completed.get("discounts") or {}).get("applied") or [])
      if isinstance(d, dict) and d.get("provisional") is True
    ]
    self.assertEmpty(
      completed_provisional,
      msg=(
        f"completed checkout after rescission must have no provisional "
        f"discounts, got {completed_provisional}"
      ),
    )


if __name__ == "__main__":
  absltest.main()

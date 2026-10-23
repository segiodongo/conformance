# Findings from linking the tests to the catalog (demo)

> **Demo only, not for merge.** These findings come from giving each of the 63
> shopping tests a marker against the catalog at `f2626c28` (see
> [Linking tests to rules](README.md#linking-tests-to-rules)). Rule IDs are the
> demo's stable IDs without the `REQ-SHOPPING-` prefix. They aren't official.

## Summary

- **Markers:** 23 tests cite rules, 23 are `no_rule`, 12 are `scenario`, 2
  are `outside_catalog` and 3 are `older_version`.
- **Rules cited:** 22, all testable: 17 MUST, 2 MUST NOT and 3 SHOULD.
- **Coverage:** the tests cite 19 of the 104 testable MUST and MUST NOT rules
  (18%): order 8 of 17, fulfillment 4 of 16 and checkout 7 of 37. Cart,
  catalog, discount and buyer consent have none.
- **Depth:** of those 19, 11 are checked in full, 6 in part, and 2 depend on
  how the rule's scope is read (see [Claims reviewed](#claims-reviewed)).
- **Sample server:** all 63 tests pass against `samples` at `360adbc`, with the
  marker check on.

## Claims reviewed

Every rule a `spec_assert` marker cites was checked against the test's
assertions. No marker changed.

- **Full:** the assertions check the rule as stated, for the case the test
  sets up.
- **Partial:** they check part of the rule; the note says which part.
- **Reading:** the claim depends on how the rule's scope is read.

| Rule             | Level    | Test                                                                                                                                                                                               | Verdict | Note                                                                                                                                                  |
| ---------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| CHECKOUT-0010    | MUST     | `ValidationTest.test_update_inventory_validation`                                                                                                                                                  | Reading | See [Rule scope](#rule-scope). Only a `quantity_adjusted` warning counts as the explanatory message.                                                  |
| CHECKOUT-0011    | MUST NOT | `ValidationTest.test_update_inventory_validation`                                                                                                                                                  | Reading | See [Rule scope](#rule-scope). A quantity changed without a message fails.                                                                            |
| CHECKOUT-0016    | MUST     | `BusinessLogicTest.test_totals_calculation_on_create`, `BusinessLogicTest.test_totals_recalculation_on_update`                                                                                     | Partial | Whole-number quantities only (1, then 2), so scale and rounding aren't exercised. On update, the line total is checked through the checkout subtotal. |
| CHECKOUT-0105    | SHOULD   | `ValidationTest`: `test_out_of_stock`, `test_update_inventory_validation`, `test_product_not_found`, `test_payment_failure`, `test_complete_without_fulfillment`, `test_structured_error_messages` | Full    | "Accurate" is checked as naming the cause: a 4xx whose body mentions it, or a standard error code.                                                    |
| CHECKOUT-0119    | SHOULD   | `CheckoutLifecycleTest.test_cannot_cancel_completed_checkout`                                                                                                                                      | Partial | Asserts only a status other than 200: an error returned in band with HTTP 200 would fail, and the error's content isn't checked.                      |
| CHECKOUT-0120    | SHOULD   | `CheckoutLifecycleTest.test_cancel_checkout`                                                                                                                                                       | Full    |                                                                                                                                                       |
| CHECKOUT-0133    | MUST     | `TotalsTest.test_single_subtotal_entry`                                                                                                                                                            | Full    |                                                                                                                                                       |
| CHECKOUT-0134    | MUST     | `TotalsTest.test_single_total_entry`, and both `BusinessLogicTest` totals tests                                                                                                                    | Full    |                                                                                                                                                       |
| CHECKOUT-0142    | MUST     | `TotalsTest.test_discount_entry_is_negative`                                                                                                                                                       | Full    | Checks `discount` and `items_discount` entries; skips when there are none.                                                                            |
| CHECKOUT-0142    | MUST     | `DiscountSemanticsTest.test_discount_total_is_negative`                                                                                                                                            | Partial | Checks only the `discount` entry.                                                                                                                     |
| CHECKOUT-0143    | MUST     | `TotalsTest.test_additive_entries_non_negative`                                                                                                                                                    | Full    |                                                                                                                                                       |
| FULFILLMENT-0012 | MUST     | `FulfillmentTest.test_known_customer_multiple_addresses_selection`                                                                                                                                 | Full    | For a destination the business listed itself (a saved address).                                                                                       |
| FULFILLMENT-0015 | MUST NOT | `FulfillmentTest.test_known_customer_multiple_addresses_selection`                                                                                                                                 | Full    | Both parts: the same ID comes back, and its destination is the selected address.                                                                      |
| FULFILLMENT-0018 | MUST     | `FulfillmentStructureTest.test_option_titles_distinguish_siblings`                                                                                                                                 | Partial | Distinct, non-empty titles. Whether a title tells options apart for a buyer can't be checked by machine.                                              |
| FULFILLMENT-0048 | MUST     | `FulfillmentStructureTest.test_default_config_single_group_per_method`                                                                                                                             | Full    |                                                                                                                                                       |
| ORDER-0008       | MUST     | `OrderTest.test_order_retrieval`                                                                                                                                                                   | Partial | Get Order only, and "full" is checked as the schema's required fields.                                                                                |
| ORDER-0018       | MUST     | `WebhookStructureTest.test_transient_failure_retries_preserve_event_identity`                                                                                                                      | Full    |                                                                                                                                                       |
| ORDER-0021       | MUST     | `WebhookStructureTest.test_signed_delivery_carries_required_headers_and_digest`                                                                                                                    | Full    |                                                                                                                                                       |
| ORDER-0022       | MUST     | `WebhookStructureTest.test_signed_delivery_carries_required_headers_and_digest`                                                                                                                    | Partial | Checks the RFC 9421 headers and the `Content-Digest`, not the signature. Skips when deliveries carry no signature headers at all.                     |
| ORDER-0023       | MUST     | `WebhookStructureTest.test_order_created_event_is_full_order_entity`                                                                                                                               | Partial | "Fully populated" is checked as the schema's required fields.                                                                                         |
| ORDER-0024       | MUST     | `WebhookStructureTest.test_update_event_is_full_order_entity`                                                                                                                                      | Partial | The same floor. The update comes from the sample server's `/testing/simulate-shipping`, so elsewhere the test skips.                                  |
| ORDER-0025       | MUST     | `WebhookStructureTest.test_transient_failure_retries_preserve_event_identity`                                                                                                                      | Full    |                                                                                                                                                       |
| ORDER-0026       | MUST     | `WebhookStructureTest.test_transient_failure_retries_preserve_event_identity`                                                                                                                      | Full    | The same rule as ORDER-0018; see [A rule stated twice](#a-rule-stated-twice).                                                                         |

## Spec findings

### Behaviour stated without a keyword

22 of the 23 `no_rule` tests check behaviour the specification describes but
doesn't state with an RFC 2119 keyword. The other, `test_repeated_cancel`, is
`no_rule` because it accepts either outcome (see
[Test findings](#test-findings)). Each is a candidate for normative wording,
unless the behaviour is meant to be informative.

| Page and section                             | What it says                                                                                                                                           | Tests                                                                                                          |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------- |
| `checkout/index.md`, Create Checkout         | "Initiates a new checkout session"; only the schema requires the response's `id`                                                                       | `test_create_checkout`                                                                                         |
| `checkout/index.md`, Get Checkout            | "It provides the latest state of the checkout resource"                                                                                                | `test_get_checkout`                                                                                            |
| `checkout/index.md`, Update Checkout         | The request "will replace the existing checkout session state on the business side". CHECKOUT-0243 (`checkout/rest.md`) says so inside a platform rule | `test_update_checkout`, `test_buyer_info_persistence`                                                          |
| `checkout/index.md`, Complete Checkout       | A synchronous completion has `status` `completed` and an `order` with `id` and `permalink_url`; it places the order "from `ready_for_complete`"        | `test_complete_checkout`, `test_cannot_complete_canceled_checkout`                                             |
| `checkout/index.md`, Status Values           | A `canceled` session "is invalid or expired"                                                                                                           | `test_cannot_update_canceled_checkout`                                                                         |
| `checkout/index.md`, Business guidelines     | A completed session "is considered immutable"                                                                                                          | `test_complete_is_idempotent`, `test_cannot_update_completed_checkout`                                         |
| `checkout/index.md`, totals invariants       | "Every entry carries a `type` and an `amount`"; "Amounts are signed integers"                                                                          | `test_entries_have_type_and_amount`                                                                            |
| `extensions/fulfillment.md`, Overview        | Checkout fulfillment covers "selection and cost"; nothing says the total includes the selected option's cost                                           | `test_fulfillment_flow`                                                                                        |
| `order/index.md`, Order Event Webhook        | A "Required Headers" table lists `Webhook-Id` and `Webhook-Timestamp`                                                                                  | `test_delivery_carries_standard_webhook_headers`                                                               |
| `extensions/discount.md`, Operations         | Submitted codes replace earlier ones, `"codes": []` clears them, and codes match case-insensitively                                                    | `test_codes_replace_previous_set`, `test_empty_codes_removes_discount`, `test_code_matches_case_insensitively` |
| `extensions/discount.md`, Operations         | `discounts.applied` lists the active discounts and `totals[]` their amounts; `applied` is listed under "Response behavior"                             | `test_discount_flow`, `test_multiple_discounts_accepted`, `test_client_applied_does_not_change_price`          |
| `extensions/discount.md`, Allocation Details | `applied[].amount` is the discount's magnitude; the allocations "Invariant"                                                                            | `test_fixed_amount_discount`, `test_allocations_sum_to_applied_amount`                                         |
| `extensions/discount.md`, Rejected Codes     | A code that can't be applied is reported in `messages[]`                                                                                               | `test_multiple_discounts_one_rejected`, `test_unknown_discount_code`                                           |

Two related observations:

- **Discount:** 10 of its 11 tests are `no_rule`. Its 9 rules cover
  discovery, cart-to-checkout continuity, how rejections are reported and
  eligibility claims, not the request and response behaviour above.
- **Errors:** `checkout/rest.md` (Error Responses) says business outcomes,
  such as unavailable merchandise, return HTTP 200 with `messages`, without a
  keyword. The message fields `type`, `code`, `content` and `severity` are
  required only by the schema.

### A rule stated twice

ORDER-0018 (Events, Webhook Signature Verification) and ORDER-0026 (Events,
Guidelines) both require retries to keep the original `Webhook-Id` and
`Webhook-Timestamp`. The retry test cites both, so neither shows as uncovered.

### Rule scope

CHECKOUT-0010 ("A revision MUST be returned as a revised line `quantity` with
an explanatory `messages[]` entry") and CHECKOUT-0011 ("MUST NOT silently
reinterpret the requested quantity") sit in the Ordering increment paragraph.
The next sentence, CHECKOUT-0012, says the business "MAY also revise a
quantity for its own reasons (for example, limited stock)". This review reads
0010 and 0011 as covering those revisions too, which matches the Business
Outcomes example in `checkout/rest.md`: a `quantity_adjusted` warning for
limited stock. Saying so explicitly would remove the doubt.

### Changes since 2026-04-08

The tests target the 2026-04-08 release; the catalog is the later draft.
Three tests check behaviour the draft has changed (`older_version`):

| Test                                                   | What changed                                                                                                                                                                                                                              |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BusinessLogicTest.test_buyer_consent`                 | Consent purposes are keyed by reverse-DNS ID, and businesses MUST ignore unadvertised ones (BUYER-CONSENT-0010). The test sends boolean flags.                                                                                            |
| `FulfillmentTest.test_known_customer_new_address`      | The new Selection and Location Identity contract (FULFILLMENT-0012 to 0017) forbids returning a different `selected_destination_id` (FULFILLMENT-0015). The test accepts one.                                                             |
| `WebhookStructureTest.test_signature_covers_ucp_agent` | ucp#659 adds `webhook-id` and `webhook-timestamp` to the components a webhook signature must cover, and fixes the order page's example. The test checks the 2026-04-08 set and skips when `ucp-agent` isn't covered, waiting on that fix. |

### Rules on pages the catalog doesn't cover

The catalog covers `docs/specification/shopping/` only. Two tests check rules
on `overview/index.md`: service advertisement, and the `version_unsupported`
error for an unsupported version. Two check rules in `signatures.md`: the
components a signature covers, and that a business's profile URL points to
`/.well-known/ucp`.

## Test findings

- **Accepts what the spec doesn't describe:** the 6 `ValidationTest` tests
  also accept a 4xx for a business outcome. `test_repeated_cancel` accepts a
  200 or an error, so it can't fail on CHECKOUT-0119.
- **Looks only for `discount` entries:** `test_unknown_discount_code`,
  `test_code_matches_case_insensitively`, `test_discount_total_is_negative`
  and `test_empty_codes_removes_discount`. The discount page reports
  line-item discounts as `items_discount`, so a business that allocates
  discounts to line items fails three of them, and passes the fourth without
  a real check.
- **Doesn't check rejection messages:** no test checks the `messages[]` entry
  for a rejected code (`discount_code_invalid`, with the `warning` type that
  DISCOUNT-0005 recommends).
- **Assumes no tax or fees:** `test_fulfillment_flow` expects the total to be
  exactly the price plus the shipping option.
- **Duplicate:** `test_structured_error_messages` sends the same request and
  makes the same assertions as `test_out_of_stock`.
- **Names that don't match:** `test_complete_is_idempotent` checks that a
  second completion is refused. In `test_signature_covers_ucp_agent`, only a
  missing `ucp-agent` skips; a missing `@method`, `@authority`, `@path`,
  `content-digest` or `content-type` fails.
- **Skips that can hide a failure:** the signature tests skip when deliveries
  carry no signature headers at all, so the worst case shows as a skip.
- **Needs fixtures:** these skip without the matching `conformance_input.json`
  entries: known customers with and without saved addresses, a
  free-shipping threshold and item, dynamic fulfillment destinations, and a
  second or fixed-amount discount code.
- **Needs the sample server:**
  - `PUT /orders/{id}`, which neither the draft nor any release defines:
    `test_order_update`, `test_order_adjustments`,
    `test_invalid_adjustment_status` and `test_malformed_adjustment_payload`,
    all marked `scenario`.
  - `POST /testing/simulate-shipping/{id}`:
    `test_update_event_is_full_order_entity` skips without it.
  - The failing payment instrument `instr_fail`: `test_payment_failure`.

## Sample server findings

`Universal-Commerce-Protocol/samples` at `360adbc`, which passes all 63 tests:

- It adds `PUT /orders/{id}` and `POST /testing/simulate-shipping/{id}`,
  which the specification doesn't define.
- It answers business outcomes such as an out-of-stock item with a 4xx (409),
  where `checkout/rest.md` describes HTTP 200 with `messages`.
- It signs webhook deliveries and covers `ucp-agent`, so the signature tests
  run instead of skipping.

## Coverage gaps

85 testable MUST and MUST NOT rules have no test: checkout 30, catalog 18
(16 for lookup and 11 for search, 9 of them shared), fulfillment 12, order 9,
cart 8, discount 4 and buyer consent 4. Each is a candidate for a new test.

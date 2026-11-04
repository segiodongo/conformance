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

"""Base integration test and payload helpers for the UCP shopping vertical.

Built on `framework.base_test.BaseIntegrationTest`, which owns the HTTP
client, the command-line flags, discovery and the mock agent profile server.
This module adds everything specific to the shopping vertical: fixtures,
checkout payload builders and the checkout/order helpers the tests call.
"""

import csv
from fractions import Fraction
import json
import logging
import math
from pathlib import Path
import re
from typing import Any, TypedDict
import urllib.parse
import uuid
from framework.base_test import (
  BaseIntegrationTest,
  flag_value,
)
from framework.decorators import requires_capability
from framework.outcomes import CAPABILITY, skip_reason
import httpx
from ucp_sdk.models.schemas import payment_handler
from ucp_sdk.models.schemas.shopping import checkout as f_models
from ucp_sdk.models.schemas.shopping import checkout_create_request
from ucp_sdk.models.schemas.shopping.checkout_update_request import (
  CheckoutUpdateRequest,
)
from ucp_sdk.models.schemas.shopping.types import (
  fulfillment_group_create_request,
  fulfillment_method_create_request,
  item_create_request,
  item_update_request,
  line_item_create_request,
  line_item_update_request,
  shipping_destination_create_request,
)

try:
  from ucp_sdk.models.schemas.shopping import (
    payment_create_request,
    payment_update_request,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types import (
    payment_create_request,
    payment_update_request,
  )

try:
  from ucp_sdk.models.schemas.shopping.payment import (
    Payment,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types.payment import (
    Payment,
  )

# Rebuild models to resolve forward references
f_models.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


def expected_line_total(line: dict[str, Any]) -> tuple[float, int]:
  """Return what checkout.md says a response line's total is, and its scale.

  "The Business MUST compute the line total as `price × quantity × 10^-scale`
  and round once at the line" (checkout.md, Pricing a line). `scale` is the
  line's effective sale basis, `item.quantity_unit.scale`, or 0 when the
  descriptor is absent (the default identity: whole items).

  Args:
    line: A checkout, cart or order line as the server returned it.

  Returns:
    The exact product, before the one rounding the rule allows, and the
    scale it was computed with.

  """
  item = line.get("item") or {}
  unit = item.get("quantity_unit") or {}
  scale = int(unit.get("scale") or 0)
  price = int(item["price"])
  quantity = int(line["quantity"])
  return price * quantity / 10**scale, scale


def line_total_entry(line: dict[str, Any]) -> int | None:
  """Return the amount of a response line's own total.

  The line's `totals[]` carries the product the rule speaks of as its
  `subtotal` entry; a line with no such entry but a lone `total` entry is read
  as that total.
  """
  entries = {
    entry.get("type"): entry.get("amount") for entry in line.get("totals") or []
  }
  if "subtotal" in entries:
    return entries["subtotal"]
  return entries.get("total") if len(entries) == 1 else None


def _is_int(value: Any) -> bool:
  """Return whether value is an int and not a bool."""
  return isinstance(value, int) and not isinstance(value, bool)


def unit_price_problems(
  unit_price: Any, price: Any, where: str = "unit_price"
) -> list[str]:
  """Return every violation of the catalog unit_price invariants, or none.

  Checks the rules of catalog/index.md (Distinction from unit price):
    - CATALOG-0003: positive integer for `measure.value` and `reference.value`.
    - CATALOG-0004: `unit_price.currency` equals `price.currency`.
    - CATALOG-0005: `measure.unit` equals `reference.unit`.
    - CATALOG-0007: no cross-unit or currency conversion.
    - CATALOG-0008: comparator is `(price.amount / (measure.value ×
      10^-measure.scale)) × (reference.value × 10^-reference.scale)`.
    - CATALOG-0009: rounded once to the currency's minor units as
      `unit_price.amount`.

  Args:
    unit_price: The `unit_price` object from a variant or transaction item.
    price: The `price` object (`{"amount", "currency"}`) it is derived from.
    where: Label for error messages.

  Returns:
    Human-readable problem descriptions; empty when all invariants hold.

  """
  if not isinstance(unit_price, dict):
    return [f"{where}: expected a dict, got {unit_price!r}"]
  if not isinstance(price, dict):
    return [f"{where}: price must be a dict, got {price!r}"]

  problems: list[str] = []
  measure = unit_price.get("measure")
  reference = unit_price.get("reference")
  if not isinstance(measure, dict):
    problems.append(f"{where}.measure: missing or not a dict ({measure!r})")
    measure = {}
  if not isinstance(reference, dict):
    problems.append(f"{where}.reference: missing or not a dict ({reference!r})")
    reference = {}

  # CATALOG-0003: positive integer for measure.value and reference.value.
  m_val = measure.get("value")
  r_val = reference.get("value")
  if not _is_int(m_val) or m_val < 1:
    problems.append(
      f"{where}.measure.value: must be a positive integer, got {m_val!r} "
      "(CATALOG-0003)"
    )
  if not _is_int(r_val) or r_val < 1:
    problems.append(
      f"{where}.reference.value: must be a positive integer, got {r_val!r} "
      "(CATALOG-0003)"
    )

  # CATALOG-0004 & CATALOG-0007: unit_price.currency == price.currency.
  up_currency = unit_price.get("currency")
  p_currency = price.get("currency")
  if (
    not isinstance(up_currency, str)
    or not up_currency
    or up_currency != p_currency
  ):
    problems.append(
      f"{where}.currency: {up_currency!r} does not equal price.currency "
      f"{p_currency!r} (CATALOG-0004, CATALOG-0007)"
    )

  # CATALOG-0005 & CATALOG-0007: measure.unit == reference.unit.
  m_unit = measure.get("unit")
  r_unit = reference.get("unit")
  if not isinstance(m_unit, str) or not m_unit or m_unit != r_unit:
    problems.append(
      f"{where}: measure.unit {m_unit!r} does not equal reference.unit "
      f"{r_unit!r} (CATALOG-0005, CATALOG-0007)"
    )

  # CATALOG-0008 & CATALOG-0009: comparator formula rounded once to minor units.
  up_amount = unit_price.get("amount")
  p_amount = price.get("amount")
  m_scale = measure.get("scale", 0)
  r_scale = reference.get("scale", 0)
  if m_scale is None:
    m_scale = 0
  if r_scale is None:
    r_scale = 0
  if not _is_int(up_amount):
    problems.append(
      f"{where}.amount: must be an integer, got {up_amount!r} (CATALOG-0009)"
    )
  elif not _is_int(p_amount):
    problems.append(f"{where}: price.amount is not an integer ({p_amount!r})")
  elif not _is_int(m_scale) or m_scale < 0:
    problems.append(f"{where}.measure.scale: invalid scale {m_scale!r}")
  elif not _is_int(r_scale) or r_scale < 0:
    problems.append(f"{where}.reference.scale: invalid scale {r_scale!r}")
  elif _is_int(m_val) and m_val >= 1 and _is_int(r_val) and r_val >= 1:
    exact = (
      Fraction(p_amount, 1)
      * Fraction(r_val, 10**r_scale)
      / Fraction(m_val, 10**m_scale)
    )
    allowed = {math.floor(exact), math.ceil(exact)}
    if up_amount not in allowed:
      problems.append(
        f"{where}.amount: got {up_amount}, expected {float(exact):.4f} "
        f"rounded once to minor units ({sorted(allowed)}) from "
        f"price.amount={p_amount}, measure={measure}, reference={reference} "
        "(CATALOG-0007, CATALOG-0008, CATALOG-0009)"
      )

  return problems


def _is_reverse_domain_claim(value: Any) -> bool:
  """Return whether `value` is a reverse-domain eligibility claim string."""
  if not isinstance(value, str):
    return False
  stripped = value.strip()
  if not stripped or stripped != value or " " in stripped:
    return False
  parts = stripped.split(".")
  return len(parts) >= 2 and all(parts)


def provisional_discount_problems(
  checkout_data: Any, claim: str, where: str = "discounts.applied"
) -> list[str]:
  """Return every violation of provisional discount rules for `claim`.

  Checks the rules of discount.md (Eligibility Claims & Discount Behavior):
    - DISCOUNT-0006: when the discount extension is active, Businesses that
      choose to accept eligibility claims MUST surface their effect on pricing
      as provisional discounts in the `applied` array.
    - DISCOUNT-0008: when a Business recognizes a claim and it affects pricing,
      it MUST surface a corresponding provisional discount in
      `discounts.applied` (`provisional: true`, `eligibility: claim`,
      `automatic: true`, `code` omitted, positive `amount`, non-empty `title`).
  """
  if not isinstance(checkout_data, dict):
    return [f"{where}: checkout response is not an object"]
  discounts = checkout_data.get("discounts")
  if not isinstance(discounts, dict):
    return [
      f"{where}: missing discounts object on checkout for claim {claim!r} "
      "(DISCOUNT-0006, DISCOUNT-0008)"
    ]
  applied = discounts.get("applied")
  if not isinstance(applied, list) or not applied:
    return [
      f"{where}: discounts.applied is empty or missing for claim {claim!r} "
      "(DISCOUNT-0006, DISCOUNT-0008)"
    ]

  matching = [
    entry
    for entry in applied
    if isinstance(entry, dict) and entry.get("eligibility") == claim
  ]
  if not matching:
    return [
      f"{where}: no entry in discounts.applied carries eligibility={claim!r}; "
      f"got {applied} (DISCOUNT-0006, DISCOUNT-0008)"
    ]

  problems: list[str] = []
  for idx, entry in enumerate(matching):
    prefix = f"{where}[eligibility={claim!r}#{idx}]"
    if entry.get("provisional") is not True:
      problems.append(
        f"{prefix}.provisional: must be True for an unverified eligibility "
        f"claim, got {entry.get('provisional')!r} "
        "(DISCOUNT-0006, DISCOUNT-0008)"
      )
    if entry.get("automatic") is not True:
      problems.append(
        f"{prefix}.automatic: must be True for an eligibility-triggered "
        f"discount, got {entry.get('automatic')!r} (DISCOUNT-0008)"
      )
    if entry.get("code") is not None:
      problems.append(
        f"{prefix}.code: must be omitted for an eligibility-triggered "
        f"discount, got {entry.get('code')!r} (DISCOUNT-0008)"
      )
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
      problems.append(f"{prefix}.title: must be a non-empty string")
    amount = entry.get("amount")
    if not _is_int(amount) or amount <= 0:
      problems.append(
        f"{prefix}.amount: must be a positive integer in minor units, got "
        f"{amount!r} (DISCOUNT-0006, DISCOUNT-0008)"
      )

  totals = checkout_data.get("totals") or []
  discount_totals = [
    t.get("amount")
    for t in totals
    if isinstance(t, dict)
    and t.get("type") in ("discount", "items_discount")
    and _is_int(t.get("amount"))
  ]
  if not discount_totals or sum(discount_totals) >= 0:
    problems.append(
      f"{where}: provisional discount for {claim!r} is not reflected as a "
      f"negative discount/items_discount entry in totals[]: {totals} "
      "(DISCOUNT-0006)"
    )

  return problems


def eligibility_invalid_error_problems(
  body: Any, where: str = "complete response"
) -> list[str]:
  """Return every violation of unverified-claim completion rules.

  Checks the rules of checkout/index.md (Eligibility Verification at
  Completion):
    - CHECKOUT-0047 & CHECKOUT-0052: Businesses MUST NOT complete a transaction
      with unresolved eligibility claims (`status != "completed"`, no `order`).
    - CHECKOUT-0051: accepted claims that remain unverified at completion MUST
      result in `type: "error"` with `code: "eligibility_invalid"`.
    - CHECKOUT-0053: the Business MUST return an error in `messages` with
      `code: "eligibility_invalid"` and `severity: "recoverable"`.
  """
  if not isinstance(body, dict):
    return [f"{where}: response body is not a JSON object ({body!r})"]

  problems: list[str] = []
  if body.get("status") == "completed" or body.get("order") is not None:
    problems.append(
      f"{where}: completed transaction despite unresolved eligibility claim "
      f"(status={body.get('status')!r}, order={body.get('order')!r}) "
      "(CHECKOUT-0047, CHECKOUT-0052)"
    )

  messages = body.get("messages")
  if not isinstance(messages, list) or not messages:
    problems.append(
      f"{where}: missing messages[] with eligibility_invalid error "
      "(CHECKOUT-0051, CHECKOUT-0053)"
    )
    return problems

  elig_msgs = [
    m
    for m in messages
    if isinstance(m, dict) and m.get("code") == "eligibility_invalid"
  ]
  if not elig_msgs:
    problems.append(
      f"{where}: no message in messages[] has code='eligibility_invalid'; "
      f"got {messages} (CHECKOUT-0051, CHECKOUT-0053)"
    )
    return problems

  for idx, msg in enumerate(elig_msgs):
    if msg.get("type") != "error":
      problems.append(
        f"{where}.messages[{idx}]: eligibility_invalid message must have "
        f"type='error', got {msg.get('type')!r} (CHECKOUT-0051)"
      )
    if msg.get("severity") != "recoverable":
      problems.append(
        f"{where}.messages[{idx}]: eligibility_invalid message must have "
        f"severity='recoverable', got {msg.get('severity')!r} "
        "(CHECKOUT-0053)"
      )

  return problems


def _extract_fulfillment_methods(checkout_data: Any) -> list[dict[str, Any]]:
  """Return `fulfillment.methods[]` from a raw checkout dict."""
  if not isinstance(checkout_data, dict):
    return []
  fulfillment = checkout_data.get("fulfillment") or {}
  if not isinstance(fulfillment, dict):
    return []
  if "methods" not in fulfillment and isinstance(fulfillment.get("root"), dict):
    fulfillment = fulfillment["root"]
  methods = fulfillment.get("methods")
  return methods if isinstance(methods, list) else []


def selected_destination_problems(
  method: Any,
  expected_id: str | None = None,
  expected_type: str | None = None,
  where: str = "fulfillment.methods[0]",
) -> list[str]:
  """Return every violation of accepted `selected_destination_id` rules.

  Checks the rules of extensions/fulfillment.md (Destination Selection and
  Business Location Destination):
    - FULFILLMENT-0012: when the Business accepts a non-null
      `selected_destination_id`, its response MUST carry that same
      `selected_destination_id` value on the method.
    - FULFILLMENT-0013: its response MUST include exactly one destination in
      that same method's `destinations[]` whose `id` equals it, typed as
      specified in Destinations.
    - FULFILLMENT-0009: when a Business Location Destination is returned on a
      `pickup` method (`type: "business_location"`), the Business MUST return
      `type: "business_location"`, `id`, and its Buyer-facing `name`.
  """
  if not isinstance(method, dict):
    return [f"{where}: method is not a JSON object ({method!r})"]

  problems: list[str] = []
  selected_id = method.get("selected_destination_id")
  if not isinstance(selected_id, str) or not selected_id.strip():
    problems.append(
      f"{where}.selected_destination_id: must be a non-empty string, got "
      f"{selected_id!r} (FULFILLMENT-0012)"
    )
    return problems

  if expected_id is not None and selected_id != expected_id:
    problems.append(
      f"{where}.selected_destination_id: expected {expected_id!r}, got "
      f"{selected_id!r} (FULFILLMENT-0012)"
    )

  destinations = method.get("destinations")
  if not isinstance(destinations, list) or not destinations:
    problems.append(
      f"{where}.destinations: must contain the selected destination "
      f"{selected_id!r}, got {destinations!r} (FULFILLMENT-0013)"
    )
    return problems

  matching = [
    d
    for d in destinations
    if isinstance(d, dict) and d.get("id") == selected_id
  ]
  if len(matching) != 1:
    problems.append(
      f"{where}.destinations: must include exactly one destination with "
      f"id={selected_id!r}, found {len(matching)} in {destinations} "
      "(FULFILLMENT-0013)"
    )
    return problems

  dest = matching[0]
  dest_type = dest.get("type")
  if not isinstance(dest_type, str) or not dest_type.strip():
    problems.append(
      f"{where}.destinations[id={selected_id!r}].type: destination in "
      f"response must carry a non-empty 'type' discriminator, got "
      f"{dest_type!r} (FULFILLMENT-0013)"
    )
  elif expected_type is not None and dest_type != expected_type:
    problems.append(
      f"{where}.destinations[id={selected_id!r}].type: expected "
      f"{expected_type!r}, got {dest_type!r} "
      "(FULFILLMENT-0009, FULFILLMENT-0013)"
    )

  if expected_type == "business_location" or dest_type == "business_location":
    name = dest.get("name")
    if not isinstance(name, str) or not name.strip():
      problems.append(
        f"{where}.destinations[id={selected_id!r}].name: business_location "
        f"destination must carry a non-empty buyer-facing 'name', got "
        f"{name!r} (FULFILLMENT-0009)"
      )

  return problems


def _selects_method_destination_path(path: Any, method_index: int = 0) -> bool:
  """Return whether `path` selects `methods[i].selected_destination_id`."""
  if not isinstance(path, str) or not path.strip():
    return False
  normalized = path.strip()
  expected_jsonpath = (
    f"$.fulfillment.methods[{method_index}].selected_destination_id"
  )
  expected_pointer = (
    f"/fulfillment/methods/{method_index}/selected_destination_id"
  )
  return normalized in (
    expected_jsonpath,
    expected_jsonpath.lstrip("$."),
    expected_pointer,
  )


def rejected_destination_update_problems(
  before_checkout: Any,
  after_checkout: Any,
  method_index: int = 0,
  where: str = "update response",
) -> list[str]:
  """Return every violation of rejected `selected_destination_id` update rules.

  Checks the rules of extensions/fulfillment.md (Destination Selection):
    - FULFILLMENT-0016: when the Business cannot accept a
      `selected_destination_id` submitted on Update Checkout (not recognized
      for that method, or revalidation fails), it MUST leave the current
      Checkout unchanged.
    - FULFILLMENT-0017: it MUST return that Checkout with an error Message
      with `severity: "recoverable"` whose `path` selects the attempted
      method's `selected_destination_id` (for example
      `$.fulfillment.methods[0].selected_destination_id`).
  """
  if not isinstance(before_checkout, dict):
    return [f"{where}: before_checkout is not a dict ({before_checkout!r})"]
  if not isinstance(after_checkout, dict):
    return [f"{where}: after_checkout is not a dict ({after_checkout!r})"]

  problems: list[str] = []
  before_methods = _extract_fulfillment_methods(before_checkout)
  after_methods = _extract_fulfillment_methods(after_checkout)
  if len(before_methods) <= method_index or len(after_methods) <= method_index:
    problems.append(
      f"{where}: missing fulfillment.methods[{method_index}] in before or "
      f"after checkout (before={len(before_methods)}, "
      f"after={len(after_methods)}) (FULFILLMENT-0016)"
    )
  else:
    before_method = before_methods[method_index]
    after_method = after_methods[method_index]
    if after_method.get("selected_destination_id") != before_method.get(
      "selected_destination_id"
    ):
      problems.append(
        f"{where}.fulfillment.methods[{method_index}].selected_destination_id: "
        f"changed from {before_method.get('selected_destination_id')!r} to "
        f"{after_method.get('selected_destination_id')!r} on a rejected update "
        "(FULFILLMENT-0016)"
      )
    if _without_empties(after_method.get("destinations")) != _without_empties(
      before_method.get("destinations")
    ):
      problems.append(
        f"{where}.fulfillment.methods[{method_index}].destinations: modified "
        "on a rejected selected_destination_id update (FULFILLMENT-0016)"
      )

  if _without_empties(after_checkout.get("totals")) != _without_empties(
    before_checkout.get("totals")
  ):
    problems.append(
      f"{where}.totals: changed from {before_checkout.get('totals')} to "
      f"{after_checkout.get('totals')} on a rejected update (FULFILLMENT-0016)"
    )

  messages = after_checkout.get("messages")
  if not isinstance(messages, list) or not messages:
    problems.append(
      f"{where}.messages: missing recoverable error message for rejected "
      f"selected_destination_id (FULFILLMENT-0017)"
    )
    return problems

  matching_msgs = [
    m
    for m in messages
    if isinstance(m, dict)
    and m.get("type") == "error"
    and m.get("severity") == "recoverable"
    and _selects_method_destination_path(m.get("path"), method_index)
  ]
  if not matching_msgs:
    problems.append(
      f"{where}.messages: no message with type='error', "
      "severity='recoverable', and path="
      f"'$.fulfillment.methods[{method_index}].selected_destination_id'; "
      f"got {messages} (FULFILLMENT-0017)"
    )

  return problems


def option_description_problems(
  option: Any, where: str = "option"
) -> list[str]:
  """Return every violation of `FULFILLMENT-0021` on an option's description.

  extensions/fulfillment.md (Rendering / Business Responsibilities):
    - FULFILLMENT-0021: For `options[].description`: MUST NOT repeat `title`
      or `total` — provides supplementary context only.
  """
  if not isinstance(option, dict):
    return [f"{where}: option is not a dict ({option!r})"]

  raw_desc = option.get("description")
  if raw_desc is None:
    return []

  desc_texts: list[str] = []
  if isinstance(raw_desc, str):
    if raw_desc.strip():
      desc_texts.append(raw_desc)
  elif isinstance(raw_desc, dict):
    for key in ("plain", "markdown"):
      val = raw_desc.get(key)
      if isinstance(val, str) and val.strip():
        desc_texts.append(val)

  if not desc_texts:
    return []

  problems: list[str] = []
  title = option.get("title")
  norm_title = (
    " ".join(title.casefold().split())
    if isinstance(title, str) and title.strip()
    else ""
  )

  total_amount: int | None = None
  for entry in option.get("totals") or []:
    if (
      isinstance(entry, dict)
      and entry.get("type") == "total"
      and _is_int(entry.get("amount"))
    ):
      total_amount = entry["amount"]
      break

  for desc in desc_texts:
    norm_desc = " ".join(desc.casefold().split())
    if norm_title and norm_title in norm_desc:
      problems.append(
        f"{where}.description: repeats option title {title!r} in {desc!r} "
        "(FULFILLMENT-0021)"
      )
    if total_amount is not None:
      major_str = f"{total_amount / 100:.2f}"
      dollar_str = f"${major_str}"
      if dollar_str in norm_desc or (
        total_amount >= 100 and major_str in norm_desc
      ):
        problems.append(
          f"{where}.description: repeats option total ({dollar_str}) in "
          f"{desc!r} (FULFILLMENT-0021)"
        )

  return problems


WELL_KNOWN_TOTAL_TYPES = frozenset(
  {
    "subtotal",
    "discount",
    "items_discount",
    "fulfillment",
    "tax",
    "fee",
    "total",
  }
)

_JSONPATH_IDENT_RE = re.compile(
  r"^[A-Za-z_\u0080-\uFFFF][A-Za-z0-9_\u0080-\uFFFF]*"
)
_JSONPATH_INT_RE = re.compile(r"^(?:0|-?[1-9][0-9]*)$")
_JSONPATH_SLICE_RE = re.compile(
  r"^(?:0|-?[1-9][0-9]*)?:(?:0|-?[1-9][0-9]*)?(?::(?:0|-?[1-9][0-9]*)?)?$"
)
_WRAPPER_ROOT_KEYS = frozenset(
  {"response", "data", "body", "checkout", "result"}
)


def unknown_total_type_problems(
  totals: Any, *, where: str = "totals"
) -> list[str]:
  """Return every problem with unknown `totals[]` types in `totals`.

  checkout/index.md (Entities > Total):
    - CHECKOUT-0132: Well-known types have default display labels as fallback;
      unknown types MUST include `display_text`.
    - CHECKOUT-0145: Unknown types MUST include `display_text` (schema-enforced)
      and the sign on the amount is self-describing.
  """
  if not isinstance(totals, list):
    return [f"{where}: totals is not a list ({totals!r})"]

  problems: list[str] = []
  unknown_entries: list[tuple[int, dict[str, Any]]] = []
  for idx, entry in enumerate(totals):
    if not isinstance(entry, dict):
      problems.append(f"{where}[{idx}]: entry is not a dict ({entry!r})")
      continue
    entry_type = entry.get("type")
    if not isinstance(entry_type, str) or not entry_type.strip():
      problems.append(
        f"{where}[{idx}].type: expected non-empty string, got {entry_type!r}"
      )
      continue
    if entry_type not in WELL_KNOWN_TOTAL_TYPES:
      unknown_entries.append((idx, entry))

  if not unknown_entries:
    problems.append(
      f"{where}: expected at least one unknown totals[].type entry outside "
      f"{sorted(WELL_KNOWN_TOTAL_TYPES)} (CHECKOUT-0132, CHECKOUT-0145)"
    )
    return problems

  for idx, entry in unknown_entries:
    entry_type = entry.get("type")
    display_text = entry.get("display_text")
    if not isinstance(display_text, str) or not display_text.strip():
      problems.append(
        f"{where}[{idx}].display_text: unknown total type {entry_type!r} must "
        f"include non-empty display_text, got {display_text!r} "
        "(CHECKOUT-0132, CHECKOUT-0145)"
      )
    if not _is_int(entry.get("amount")):
      problems.append(
        f"{where}[{idx}].amount: unknown total type {entry_type!r} must carry "
        f"an integer amount, got {entry.get('amount')!r} (CHECKOUT-0145)"
      )

  return problems


def total_sub_lines_problems(
  totals: Any, *, where: str = "totals"
) -> list[str]:
  """Return every problem with `totals[].lines[]` breakdowns in `totals`.

  checkout/index.md (Entities > Total > Sub-Lines):
    - CHECKOUT-0148: Invariant: `sum(lines[].amount)` MUST equal the parent
      entry's `amount`.
  """
  if not isinstance(totals, list):
    return [f"{where}: totals is not a list ({totals!r})"]

  problems: list[str] = []
  entries_with_lines: list[tuple[int, dict[str, Any]]] = []
  for idx, entry in enumerate(totals):
    if isinstance(entry, dict) and entry.get("lines") is not None:
      entries_with_lines.append((idx, entry))

  if not entries_with_lines:
    return [
      f"{where}: expected at least one totals[] entry with a lines[] breakdown "
      "(CHECKOUT-0148)"
    ]

  for idx, entry in entries_with_lines:
    parent_amount = entry.get("amount")
    if not _is_int(parent_amount):
      problems.append(
        f"{where}[{idx}].amount: expected integer parent amount, got "
        f"{parent_amount!r}"
      )
    lines = entry.get("lines")
    if not isinstance(lines, list) or not lines:
      problems.append(
        f"{where}[{idx}].lines: expected non-empty list, got {lines!r} "
        "(CHECKOUT-0148)"
      )
      continue

    line_amounts: list[int] = []
    for sub_idx, sub in enumerate(lines):
      if not isinstance(sub, dict):
        problems.append(
          f"{where}[{idx}].lines[{sub_idx}]: expected dict, got {sub!r}"
        )
        continue
      sub_text = sub.get("display_text")
      if not isinstance(sub_text, str) or not sub_text.strip():
        problems.append(
          f"{where}[{idx}].lines[{sub_idx}].display_text: expected non-empty "
          f"string, got {sub_text!r}"
        )
      sub_amount = sub.get("amount")
      if not _is_int(sub_amount):
        problems.append(
          f"{where}[{idx}].lines[{sub_idx}].amount: expected integer, got "
          f"{sub_amount!r}"
        )
      else:
        line_amounts.append(sub_amount)

    if _is_int(parent_amount) and len(line_amounts) == len(lines):
      sub_sum = sum(line_amounts)
      if sub_sum != parent_amount:
        problems.append(
          f"{where}[{idx}].lines: sum(lines[].amount) == {sub_sum} does not "
          f"equal parent amount {parent_amount} (CHECKOUT-0148)"
        )

  return problems


def _consume_quoted_literal(text: str, pos: int) -> tuple[int, str | None]:
  """Consume a single- or double-quoted RFC 9535 string literal at `pos`."""
  quote = text[pos]
  i = pos + 1
  chars: list[str] = []
  while i < len(text):
    ch = text[i]
    if ch == "\\":
      if i + 1 >= len(text):
        return -1, None
      chars.append(text[i + 1])
      i += 2
      continue
    if ch == quote:
      return i + 1, "".join(chars)
    if ord(ch) < 0x20:
      return -1, None
    chars.append(ch)
    i += 1
  return -1, None


def _parse_bracket_segment(
  text: str, pos: int
) -> tuple[int, str | None, str | None]:
  """Parse a `[...]` segment starting at `pos` (`text[pos] == '['`).

  Returns `(next_pos, first_member_name_if_single, error_reason)`.
  """
  n = len(text)
  i = pos + 1
  first_member: str | None = None
  selector_count = 0

  while True:
    while i < n and text[i] in " \t\n\r":
      i += 1
    if i >= n:
      return -1, None, "unclosed '[' in JSONPath expression"
    if text[i] == "]" and selector_count == 0:
      return -1, None, "empty bracket segment '[]' is not valid RFC 9535"

    selector_count += 1
    ch = text[i]
    if ch in ("'", '"'):
      next_i, literal = _consume_quoted_literal(text, i)
      if next_i < 0 or literal is None:
        return -1, None, "unterminated or invalid string literal in '[...]'"
      first_member = literal if selector_count == 1 else None
      i = next_i
    elif ch == "*":
      first_member = None
      i += 1
    elif ch == "?":
      first_member = None
      i += 1
      depth_paren = 0
      depth_brack = 0
      start_expr = i
      while i < n:
        c = text[i]
        if c in ("'", '"'):
          next_i, _ = _consume_quoted_literal(text, i)
          if next_i < 0:
            return -1, None, "unterminated string literal in filter selector"
          i = next_i
          continue
        if c == "(":
          depth_paren += 1
        elif c == ")":
          depth_paren -= 1
          if depth_paren < 0:
            return -1, None, "unbalanced ')' in filter selector"
        elif c == "[":
          depth_brack += 1
        elif c == "]":
          if depth_brack == 0 and depth_paren == 0:
            break
          depth_brack -= 1
          if depth_brack < 0:
            return -1, None, "unbalanced ']' in filter selector"
        elif c == "," and depth_brack == 0 and depth_paren == 0:
          break
        i += 1
      expr = text[start_expr:i].strip()
      if not expr or depth_paren != 0 or depth_brack != 0:
        return -1, None, "invalid or unbalanced filter selector in '[...]'"
      if "@" not in expr and "$" not in expr:
        return (
          -1,
          None,
          "filter selector must reference '@' or '$' per RFC 9535",
        )
    else:
      first_member = None
      start_tok = i
      while i < n and text[i] not in ",] \t\n\r":
        i += 1
      token = text[start_tok:i]
      if not token:
        return -1, None, f"unexpected character {text[i]!r} in '[...]'"
      if ":" in token:
        if not _JSONPATH_SLICE_RE.match(token):
          return -1, None, f"invalid slice selector {token!r} in '[...]'"
      elif not _JSONPATH_INT_RE.match(token):
        return -1, None, f"invalid selector {token!r} in '[...]'"

    while i < n and text[i] in " \t\n\r":
      i += 1
    if i >= n:
      return -1, None, "unclosed '[' in JSONPath expression"
    if text[i] == ",":
      i += 1
      continue
    if text[i] == "]":
      return i + 1, first_member, None
    return -1, None, f"unexpected character {text[i]!r} inside '[...]'"


def rfc9535_jsonpath_problems(
  path_value: Any,
  response_body: dict[str, Any] | None = None,
  *,
  where: str = "messages[].path",
) -> list[str]:
  """Return every problem with a `messages[].path` value under CHECKOUT-0044.

  checkout/index.md (Error Handling > The `path` Field):
    - CHECKOUT-0044: `path` MUST be an RFC 9535 JSONPath expression relative to
      the root of the UCP response object.
  """
  if not isinstance(path_value, str) or not path_value:
    return [
      f"{where}: expected non-empty RFC 9535 JSONPath string, got "
      f"{path_value!r} (CHECKOUT-0044)"
    ]
  if path_value != path_value.strip():
    return [
      f"{where}: JSONPath must not have leading or trailing whitespace, got "
      f"{path_value!r} (CHECKOUT-0044)"
    ]
  if not path_value.startswith("$"):
    return [
      f"{where}: RFC 9535 JSONPath must start with root identifier '$', got "
      f"{path_value!r} (CHECKOUT-0044)"
    ]

  i = 1
  n = len(path_value)
  first_root_member: str | None = None
  segment_index = 0

  while i < n:
    segment_index += 1
    if path_value.startswith("..", i):
      i += 2
      if i >= n:
        return [
          f"{where}: trailing '..' in JSONPath {path_value!r} (CHECKOUT-0044)"
        ]
      if path_value[i] == "*":
        i += 1
      elif path_value[i] == "[":
        next_i, _, err = _parse_bracket_segment(path_value, i)
        if next_i < 0:
          return [f"{where}: {err} in {path_value!r} (CHECKOUT-0044)"]
        i = next_i
      else:
        m = _JSONPATH_IDENT_RE.match(path_value[i:])
        if not m:
          return [
            f"{where}: invalid descendant segment at offset {i} in "
            f"{path_value!r} (CHECKOUT-0044)"
          ]
        i += len(m.group(0))
    elif path_value[i] == ".":
      i += 1
      if i >= n:
        return [
          f"{where}: trailing '.' in JSONPath {path_value!r} (CHECKOUT-0044)"
        ]
      if path_value[i] == "*":
        i += 1
      else:
        m = _JSONPATH_IDENT_RE.match(path_value[i:])
        if not m:
          return [
            f"{where}: invalid member-name shorthand at offset {i} in "
            f"{path_value!r} (CHECKOUT-0044)"
          ]
        ident = m.group(0)
        if segment_index == 1:
          first_root_member = ident
        i += len(ident)
    elif path_value[i] == "[":
      next_i, member, err = _parse_bracket_segment(path_value, i)
      if next_i < 0:
        return [f"{where}: {err} in {path_value!r} (CHECKOUT-0044)"]
      if segment_index == 1 and member is not None:
        first_root_member = member
      i = next_i
    else:
      return [
        f"{where}: unexpected character {path_value[i]!r} at offset {i} in "
        f"{path_value!r} (CHECKOUT-0044)"
      ]

  if (
    first_root_member in _WRAPPER_ROOT_KEYS
    and isinstance(response_body, dict)
    and first_root_member not in response_body
  ):
    return [
      f"{where}: path {path_value!r} wraps the UCP response object in "
      f"{first_root_member!r} instead of being relative to the root of the "
      "UCP response object (CHECKOUT-0044)"
    ]

  return []


def absolute_https_url_problems(
  url_value: Any, *, where: str = "continue_url"
) -> list[str]:
  """Return every problem with an absolute HTTPS URL under CHECKOUT-0094.

  checkout/index.md (Continue URL > Format):
    - CHECKOUT-0094: The `continue_url` MUST be an absolute HTTPS URL.
  """
  if not isinstance(url_value, str) or not url_value.strip():
    return [
      f"{where}: expected non-empty absolute HTTPS URL string, got "
      f"{url_value!r} (CHECKOUT-0094)"
    ]
  if any(ch.isspace() for ch in url_value):
    return [
      f"{where}: URL must not contain whitespace, got {url_value!r} "
      "(CHECKOUT-0094)"
    ]
  parsed = urllib.parse.urlsplit(url_value)
  problems: list[str] = []
  if parsed.scheme.lower() != "https":
    problems.append(
      f"{where}: URL scheme must be 'https', got {parsed.scheme!r} in "
      f"{url_value!r} (CHECKOUT-0094)"
    )
  if not parsed.netloc:
    problems.append(
      f"{where}: URL must be absolute with a host (netloc), got {url_value!r} "
      "(CHECKOUT-0094)"
    )
  return problems


def disclosure_warning_problems(
  body: Any,
  *,
  require_policy_link: bool = False,
  where: str = "checkout",
) -> list[str]:
  """Return every problem with disclosure warnings in `body`.

  checkout/index.md (Warning Presentation > `disclosure`):
    - CHECKOUT-0083: MUST set `presentation: "disclosure"` when the warning
      content must be displayed alongside a specific component and must not be
      hidden or auto-dismissed.
    - CHECKOUT-0086: When the disclosure renders a structured `policies[]`
      entry, the `code` MUST equal that policy's `type` to link the two.
  """
  if not isinstance(body, dict):
    return [f"{where}: response body is not a dict ({body!r})"]

  messages = body.get("messages")
  if not isinstance(messages, list) or not messages:
    return [
      f"{where}.messages: expected non-empty messages[] with a disclosure "
      f"warning, got {messages!r} (CHECKOUT-0083)"
    ]

  disclosures: list[tuple[int, dict[str, Any]]] = []
  for idx, msg in enumerate(messages):
    if (
      isinstance(msg, dict)
      and msg.get("type") == "warning"
      and msg.get("presentation") == "disclosure"
    ):
      disclosures.append((idx, msg))

  if not disclosures:
    return [
      f"{where}.messages: expected at least one warning with "
      f"presentation == 'disclosure', got {messages!r} (CHECKOUT-0083)"
    ]

  problems: list[str] = []
  for idx, msg in disclosures:
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
      problems.append(
        f"{where}.messages[{idx}].content: disclosure warning must have "
        f"non-empty content, got {content!r} (CHECKOUT-0083)"
      )
    path_val = msg.get("path")
    if not isinstance(path_val, str) or not path_val.strip():
      problems.append(
        f"{where}.messages[{idx}].path: component-anchored disclosure must "
        f"set a non-empty path, got {path_val!r} (CHECKOUT-0083)"
      )
    else:
      problems.extend(
        rfc9535_jsonpath_problems(
          path_val, body, where=f"{where}.messages[{idx}].path"
        )
      )

  if require_policy_link:
    policies = body.get("policies")
    if not isinstance(policies, list) or not policies:
      problems.append(
        f"{where}.policies: expected non-empty policies[] to link with "
        f"disclosure warning, got {policies!r} (CHECKOUT-0086)"
      )
      return problems

    policy_types: set[str] = set()
    for p_idx, pol in enumerate(policies):
      if not isinstance(pol, dict):
        problems.append(
          f"{where}.policies[{p_idx}]: expected dict, got {pol!r} "
          "(CHECKOUT-0086)"
        )
        continue
      pol_type = pol.get("type")
      if not isinstance(pol_type, str) or not pol_type.strip():
        problems.append(
          f"{where}.policies[{p_idx}].type: expected non-empty string, got "
          f"{pol_type!r} (CHECKOUT-0086)"
        )
      else:
        policy_types.add(pol_type)

    if policy_types:
      matched = [
        msg
        for _, msg in disclosures
        if isinstance(msg.get("code"), str) and msg.get("code") in policy_types
      ]
      if not matched:
        disclosure_codes = [msg.get("code") for _, msg in disclosures]
        problems.append(
          f"{where}.messages: no disclosure warning has code matching a "
          f"policies[].type in {sorted(policy_types)} (disclosure codes: "
          f"{disclosure_codes!r}) (CHECKOUT-0086)"
        )

  return problems


def _without_empties(value: Any) -> Any:
  """Return a JSON value with its null and empty members left out.

  An omitted `messages` and `"messages": []` say the same thing; comparing
  snapshots shouldn't tell them apart. A member that is empty once its own
  empties are left out (`{"events": []}`) goes the same way.
  """
  if isinstance(value, dict):
    kept = {}
    for key, member in value.items():
      cleaned = _without_empties(member)
      if cleaned is None or cleaned == [] or cleaned == {}:
        continue
      kept[key] = cleaned
    return kept
  if isinstance(value, list):
    return [_without_empties(member) for member in value]
  return value


def snapshot_differences(
  expected: Any, actual: Any, path: str = "$", reference: str = "Get Order"
) -> list[str]:
  """Return where two entity snapshots differ, as JSON paths with the values.

  Args:
    expected: The authoritative entity, as the reference operation returns
      it.
    actual: The entity another response or delivery carried.
    path: The JSON path of the values compared, for the messages.
    reference: What returned `expected`, named in the messages.

  Returns:
    One line per difference; none when the snapshots are the same once
    null and empty members are left out of both.

  """
  expected, actual = _without_empties(expected), _without_empties(actual)
  if isinstance(expected, dict) and isinstance(actual, dict):
    found = []
    for key in sorted(set(expected) | set(actual)):
      member = f"{path}.{key}"
      if key not in actual:
        found.append(f"{member}: missing ({reference} has {expected[key]!r})")
      elif key not in expected:
        found.append(f"{member}: extra ({actual[key]!r}; {reference} has none)")
      else:
        found += snapshot_differences(
          expected[key], actual[key], member, reference
        )
    return found
  if isinstance(expected, list) and isinstance(actual, list):
    if len(expected) != len(actual):
      return [f"{path}: {len(actual)} entries, {reference} has {len(expected)}"]
    return [
      line
      for index, (want, got) in enumerate(zip(expected, actual, strict=True))
      for line in snapshot_differences(want, got, f"{path}[{index}]", reference)
    ]
  if expected != actual:
    return [f"{path}: {actual!r}, {reference} has {expected!r}"]
  return []


def complete_in_progress_problems(
  body: Any, where: str = "complete response"
) -> list[str]:
  """Return every problem with a `complete_in_progress` response.

  checkout/index.md (Status Values > `complete_in_progress`):
    - CHECKOUT-0021: The response MUST NOT contain `order`.
  """
  if not isinstance(body, dict):
    return [f"{where}: response body is not a dict ({body!r})"]

  problems: list[str] = []
  status = body.get("status")
  if status != "complete_in_progress":
    problems.append(
      f"{where}.status: expected 'complete_in_progress', got {status!r} "
      "(CHECKOUT-0021)"
    )
  if body.get("order") is not None:
    problems.append(
      f"{where}.order: complete_in_progress response MUST NOT contain "
      f"'order', got {body.get('order')!r} (CHECKOUT-0021)"
    )
  return problems


def update_during_complete_in_progress_problems(
  before_checkout: Any,
  after_checkout: Any,
  where: str = "update response",
) -> list[str]:
  """Return every problem with a rejected Update during `complete_in_progress`.

  checkout/index.md (Actions > Accepted completion) and checkout/rest.md:
    - CHECKOUT-0028 / CHECKOUT-0245: If the Business receives a new Update
      Checkout request in `complete_in_progress`, it MUST leave the Checkout
      unchanged and return the current Checkout with a recoverable error
      Message.
  """
  if not isinstance(before_checkout, dict):
    return [f"{where}: before_checkout is not a dict ({before_checkout!r})"]
  if not isinstance(after_checkout, dict):
    return [f"{where}: after_checkout is not a dict ({after_checkout!r})"]

  problems: list[str] = []
  if before_checkout.get("status") != "complete_in_progress":
    problems.append(
      f"{where}: prior checkout status must be 'complete_in_progress', got "
      f"{before_checkout.get('status')!r} (CHECKOUT-0028, CHECKOUT-0245)"
    )
  if after_checkout.get("status") != "complete_in_progress":
    problems.append(
      f"{where}.status: rejected update must return current Checkout in "
      f"'complete_in_progress', got {after_checkout.get('status')!r} "
      "(CHECKOUT-0028, CHECKOUT-0245)"
    )
  if after_checkout.get("order") is not None:
    problems.append(
      f"{where}.order: complete_in_progress response MUST NOT contain "
      f"'order', got {after_checkout.get('order')!r} (CHECKOUT-0021)"
    )

  for field_name in (
    "id",
    "currency",
    "line_items",
    "totals",
    "buyer",
    "fulfillment",
    "discounts",
    "context",
  ):
    diffs = snapshot_differences(
      before_checkout.get(field_name),
      after_checkout.get(field_name),
      path=f"{where}.{field_name}",
      reference="prior complete_in_progress checkout",
    )
    if diffs:
      for diff in diffs:
        problems.append(f"{diff} (CHECKOUT-0028, CHECKOUT-0245)")

  messages = after_checkout.get("messages")
  if not isinstance(messages, list) or not messages:
    problems.append(
      f"{where}.messages: expected recoverable error Message when Update "
      f"Checkout is received during complete_in_progress, got {messages!r} "
      "(CHECKOUT-0028, CHECKOUT-0245)"
    )
    return problems

  recoverable_errors = [
    m
    for m in messages
    if isinstance(m, dict)
    and m.get("type") == "error"
    and m.get("severity") == "recoverable"
    and isinstance(m.get("content"), str)
    and bool(m.get("content", "").strip())
  ]
  if not recoverable_errors:
    problems.append(
      f"{where}.messages: expected at least one error message with "
      f"severity == 'recoverable' and non-empty content, got {messages!r} "
      "(CHECKOUT-0028, CHECKOUT-0245)"
    )
  return problems


def requires_escalation_problems(
  body: Any,
  *,
  check_continue_url: bool = True,
  check_buyer_message: bool = True,
  where: str = "checkout",
) -> list[str]:
  """Return every problem with a `requires_escalation` checkout response.

  checkout/index.md (Continue URL > Availability, Guidelines > Business):
    - CHECKOUT-0091 / CHECKOUT-0107: MUST provide `continue_url` when returning
      `status` = `requires_escalation`.
    - CHECKOUT-0108: MUST include at least one message with `severity` of
      `requires_buyer_input` or `requires_buyer_review` when returning
      `status` = `requires_escalation`.
  """
  if not isinstance(body, dict):
    return [f"{where}: response body is not a dict ({body!r})"]

  problems: list[str] = []
  status = body.get("status")
  if status != "requires_escalation":
    problems.append(
      f"{where}.status: expected 'requires_escalation', got {status!r} "
      "(CHECKOUT-0091, CHECKOUT-0107, CHECKOUT-0108)"
    )

  if check_continue_url:
    continue_url = body.get("continue_url")
    if not isinstance(continue_url, str) or not continue_url.strip():
      problems.append(
        f"{where}.continue_url: MUST provide 'continue_url' when status == "
        f"'requires_escalation', got {continue_url!r} "
        "(CHECKOUT-0091, CHECKOUT-0107)"
      )
    else:
      url_problems = absolute_https_url_problems(
        continue_url, where=f"{where}.continue_url"
      )
      for prob in url_problems:
        problems.append(f"{prob} (CHECKOUT-0091, CHECKOUT-0107)")

  if check_buyer_message:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
      problems.append(
        f"{where}.messages: MUST include at least one message with severity "
        f"'requires_buyer_input' or 'requires_buyer_review' when status == "
        f"'requires_escalation', got {messages!r} (CHECKOUT-0108)"
      )
    else:
      buyer_msgs = [
        m
        for m in messages
        if isinstance(m, dict)
        and m.get("severity")
        in ("requires_buyer_input", "requires_buyer_review")
        and isinstance(m.get("content"), str)
        and bool(m.get("content", "").strip())
      ]
      if not buyer_msgs:
        problems.append(
          f"{where}.messages: expected at least one message with severity "
          f"'requires_buyer_input' or 'requires_buyer_review' and non-empty "
          f"content, got {messages!r} (CHECKOUT-0108)"
        )

  return problems


_ACTION_OCCURRENCE_PATH_RE = re.compile(
  r"^\$(?:\.actions|\['actions'\]|\[\"actions\"\])"
  r"(?:\['([^'\[\]]+)'\]|\[\"([^\"\[\]]+)\"\]|\.([A-Za-z_][A-Za-z0-9_]*))"
  r"\[(-?\d+|\?@\.id\s*==\s*(?:'[^']+'|\"[^\"]+\"))\]$"
)


def outstanding_actions_problems(
  body: Any, where: str = "response"
) -> list[str]:
  """Return problems when `body.actions` does not hold a valid Action."""
  if not isinstance(body, dict):
    return [f"{where} must be a JSON object, got {type(body).__name__}"]
  actions = body.get("actions")
  if not isinstance(actions, dict) or not actions:
    return [
      f"{where}.actions: expected a non-empty map of reverse-domain Action "
      f"types to instance arrays, got {actions!r}"
    ]

  problems: list[str] = []
  seen_ids: set[str] = set()
  for action_type, instances in actions.items():
    if not _is_reverse_domain_claim(action_type):
      problems.append(
        f"{where}.actions[{action_type!r}]: Action type key must be a "
        f"reverse-domain name"
      )
    if not isinstance(instances, list) or not instances:
      problems.append(
        f"{where}.actions[{action_type!r}]: must be a non-empty array of "
        f"Action instances, got {instances!r}"
      )
      continue
    for idx, inst in enumerate(instances):
      label = f"{where}.actions[{action_type!r}][{idx}]"
      if not isinstance(inst, dict):
        problems.append(f"{label}: must be a JSON object, got {inst!r}")
        continue
      inst_id = inst.get("id")
      if inst_id is not None:
        if not isinstance(inst_id, str) or not inst_id.strip():
          problems.append(
            f"{label}.id: must be a non-empty string, got {inst_id!r}"
          )
        elif inst_id in seen_ids:
          problems.append(
            f"{label}.id: duplicate Action instance id {inst_id!r} in response"
          )
        else:
          seen_ids.add(inst_id)
  return problems


def action_occurrence_path_problems(
  path: Any, body: Any, where: str = "messages[].path"
) -> list[str]:
  """Return problems for `path` selecting an exact Action occurrence.

  Checks:
  - `REQ-SHOPPING-CHECKOUT-0023` and `REQ-SHOPPING-CHECKOUT-0054`: when an
    Action prevents the requested effect (including Complete Checkout), the
    Business MUST set that error Message's `path` to select the exact Action
    occurrence in `actions` (e.g. `$.actions['com.example.verify'][0]`).
  """
  problems = rfc9535_jsonpath_problems(path, body, where=where)
  if problems:
    return [f"{p} (CHECKOUT-0023, CHECKOUT-0054)" for p in problems]
  assert isinstance(path, str)

  match = _ACTION_OCCURRENCE_PATH_RE.match(path)
  if match is None:
    return [
      f"{where}: MUST select an exact Action occurrence under $.actions "
      f"(e.g. \"$.actions['<type>'][0]\"), got {path!r} "
      f"(CHECKOUT-0023, CHECKOUT-0054)"
    ]

  action_type = match.group(1) or match.group(2) or match.group(3)
  selector = match.group(4)

  if not isinstance(body, dict):
    return [
      f"{where}: response body must be a JSON object "
      f"(CHECKOUT-0023, CHECKOUT-0054)"
    ]
  actions = body.get("actions")
  if not isinstance(actions, dict) or action_type not in actions:
    return [
      f"{where}: Action type {action_type!r} selected by {path!r} is not "
      f"present in response actions {actions!r} (CHECKOUT-0023, CHECKOUT-0054)"
    ]
  instances = actions.get(action_type)
  if not isinstance(instances, list) or not instances:
    return [
      f"{where}: response actions[{action_type!r}] is not a non-empty array "
      f"(CHECKOUT-0023, CHECKOUT-0054)"
    ]

  if selector.startswith("?"):
    quoted = selector.split("==", 1)[1].strip()
    target_id = quoted[1:-1]
    matched = [
      inst
      for inst in instances
      if isinstance(inst, dict) and inst.get("id") == target_id
    ]
    if len(matched) != 1:
      return [
        f"{where}: filter selector in {path!r} matched {len(matched)} "
        f"Action instances in actions[{action_type!r}], expected 1 "
        f"(CHECKOUT-0023, CHECKOUT-0054)"
      ]
  else:
    idx = int(selector)
    if not (-len(instances) <= idx < len(instances)):
      return [
        f"{where}: index {idx} in {path!r} is out of bounds for "
        f"actions[{action_type!r}] of length {len(instances)} "
        f"(CHECKOUT-0023, CHECKOUT-0054)"
      ]

  return []


def cart_action_non_blocking_problems(
  before_cart: Any,
  after_cart: Any,
  expected_bag: list[tuple[Any, Any]],
  where: str = "PUT /carts/{id}",
) -> list[str]:
  """Return problems for `CART-0010` when updating a cart with an Action.

  Checks:
  - `REQ-SHOPPING-CART-0010`: the Business MUST NOT treat an outstanding Action
    as a reason to reject an unrelated cart operation.
  """
  problems = outstanding_actions_problems(before_cart, where="initial cart")
  if not isinstance(after_cart, dict):
    problems.append(
      f"{where} must return a JSON object, got {type(after_cart).__name__} "
      f"(CART-0010)"
    )
    return problems

  if isinstance(before_cart, dict) and after_cart.get("id") != before_cart.get(
    "id"
  ):
    problems.append(
      f"{where}.id: expected {before_cart.get('id')!r}, got "
      f"{after_cart.get('id')!r} (CART-0010)"
    )

  actual_bag = sorted(
    ((line.get("item") or {}).get("id"), line.get("quantity"))
    for line in (after_cart.get("line_items") or [])
    if isinstance(line, dict)
  )
  if actual_bag != sorted(expected_bag):
    problems.append(
      f"{where}.line_items: unrelated cart update was not applied while an "
      f"Action was outstanding; expected items {sorted(expected_bag)!r}, got "
      f"{actual_bag!r} (CART-0010)"
    )

  error_msgs = [
    m
    for m in (after_cart.get("messages") or [])
    if isinstance(m, dict) and m.get("type") == "error"
  ]
  if error_msgs:
    problems.append(
      f"{where}.messages: MUST NOT reject an unrelated cart operation because "
      f"of an outstanding Action, got error messages {error_msgs!r} (CART-0010)"
    )

  return problems


def action_surfaced_before_complete_problems(
  pre_complete_checkout: Any,
  complete_response_checkout: Any,
  where: str = "checkout",
) -> list[str]:
  """Return problems for `CHECKOUT-0032` on Action requiring input.

  Checks:
  - `REQ-SHOPPING-CHECKOUT-0032`: because Update Checkout is unavailable after
    Complete Checkout is accepted, the Business MUST surface any Action that
    requires input through Update Checkout before accepting Complete Checkout.
  """
  problems: list[str] = []
  action_errs = outstanding_actions_problems(
    pre_complete_checkout, where=f"{where} before Complete Checkout"
  )
  if action_errs:
    problems.extend(f"{e} (CHECKOUT-0032)" for e in action_errs)

  if isinstance(pre_complete_checkout, dict):
    pre_status = pre_complete_checkout.get("status")
    if pre_status != "incomplete":
      problems.append(
        f"{where} before Complete Checkout: expected status 'incomplete' "
        f"while an Action requiring input is outstanding, got {pre_status!r} "
        f"(CHECKOUT-0032)"
      )

  if not isinstance(complete_response_checkout, dict):
    problems.append(
      f"{where} on Complete Checkout: must be a JSON object, got "
      f"{type(complete_response_checkout).__name__} (CHECKOUT-0032)"
    )
    return problems

  post_status = complete_response_checkout.get("status")
  if post_status in ("complete_in_progress", "completed"):
    problems.append(
      f"{where} on Complete Checkout: Business MUST NOT accept Complete "
      f"Checkout (status {post_status!r}) while an Action requiring input "
      f"through Update Checkout remains outstanding (CHECKOUT-0032)"
    )
  if complete_response_checkout.get("order") is not None:
    problems.append(
      f"{where} on Complete Checkout: order MUST NOT be created while an "
      f"Action requiring input remains outstanding, got "
      f"{complete_response_checkout.get('order')!r} (CHECKOUT-0032)"
    )
  pre_actions = (
    pre_complete_checkout.get("actions")
    if isinstance(pre_complete_checkout, dict)
    else None
  )
  post_actions = complete_response_checkout.get("actions")
  if isinstance(pre_actions, dict) and isinstance(post_actions, dict):
    unsurfaced = sorted(set(post_actions) - set(pre_actions))
    if unsurfaced:
      problems.append(
        f"{where} on Complete Checkout: Action types {unsurfaced!r} were not "
        f"surfaced before Complete Checkout (CHECKOUT-0032)"
      )

  return problems


def action_prevents_complete_problems(
  before_checkout: Any,
  complete_body: Any,
  where: str = "POST complete response",
) -> list[str]:
  """Return problems for `CHECKOUT-0023` and `CHECKOUT-0054` on Complete.

  Checks:
  - `REQ-SHOPPING-CHECKOUT-0023`: if an Action prevents Complete Checkout from
    being accepted, the Business MUST return the current Checkout with
    `status: incomplete` and an error Message with `severity: "recoverable"`
    whose `path` selects that exact Action occurrence.
  - `REQ-SHOPPING-CHECKOUT-0054`: when an Action prevents the requested effect,
    the Business MUST set that error Message's `path` to select the exact
    Action occurrence as specified in Actions.
  """
  if not isinstance(complete_body, dict):
    return [
      f"{where} must be a JSON object, got {type(complete_body).__name__} "
      f"(CHECKOUT-0023, CHECKOUT-0054)"
    ]

  problems: list[str] = []
  status = complete_body.get("status")
  if status != "incomplete":
    problems.append(
      f"{where}.status: expected 'incomplete' when an Action prevents Complete "
      f"Checkout, got {status!r} (CHECKOUT-0023)"
    )
  if complete_body.get("order") is not None:
    problems.append(
      f"{where}.order: MUST NOT contain an order when an Action prevents "
      f"Complete Checkout, got {complete_body.get('order')!r} (CHECKOUT-0023)"
    )

  if isinstance(before_checkout, dict) and complete_body.get(
    "id"
  ) != before_checkout.get("id"):
    problems.append(
      f"{where}.id: expected current Checkout id "
      f"{before_checkout.get('id')!r}, got {complete_body.get('id')!r} "
      f"(CHECKOUT-0023)"
    )

  action_errs = outstanding_actions_problems(complete_body, where=where)
  if action_errs:
    problems.extend(f"{e} (CHECKOUT-0023)" for e in action_errs)

  messages = complete_body.get("messages")
  if not isinstance(messages, list) or not messages:
    problems.append(
      f"{where}.messages: expected at least one recoverable error Message "
      f"selecting the Action occurrence, got {messages!r} "
      f"(CHECKOUT-0023, CHECKOUT-0054)"
    )
    return problems

  recoverable_errors = [
    m
    for m in messages
    if isinstance(m, dict)
    and m.get("type") == "error"
    and m.get("severity") == "recoverable"
    and isinstance(m.get("content"), str)
    and bool(m.get("content", "").strip())
  ]
  if not recoverable_errors:
    problems.append(
      f"{where}.messages: expected at least one message with type=='error', "
      f"severity=='recoverable', and non-empty content, got {messages!r} "
      f"(CHECKOUT-0023)"
    )
    return problems

  matching_path_found = False
  path_errors: list[str] = []
  for idx, msg in enumerate(recoverable_errors):
    p_errs = action_occurrence_path_problems(
      msg.get("path"),
      complete_body,
      where=f"{where}.messages[{idx}].path",
    )
    if not p_errs:
      matching_path_found = True
      break
    path_errors.extend(p_errs)

  if not matching_path_found:
    problems.extend(path_errors)

  return problems


def quantity_unit_machine_identity(
  descriptor: Any,
) -> tuple[str, int] | None:
  """Return the `(unit, effective_scale)` machine identity of a unit descriptor.

  An absent (`None`) `quantity_unit` descriptor represents the default identity
  `("C62", 0)`. Returns `None` when `descriptor` is malformed.
  """
  if descriptor is None:
    return ("C62", 0)
  if not isinstance(descriptor, dict):
    return None
  unit = descriptor.get("unit")
  if not isinstance(unit, str) or not unit.strip():
    return None
  scale = descriptor.get("scale", 0)
  if scale is None:
    scale = 0
  if not _is_int(scale) or scale < 0 or scale > 15:
    return None
  return (unit.strip(), scale)


def non_default_quantity_unit_problems(
  line: Any, where: str = "line_item"
) -> list[str]:
  """Return problems for `CHECKOUT-0015` on a non-default sale-basis line.

  Checks:
  - `REQ-SHOPPING-CHECKOUT-0015`: the Business MUST include `quantity_unit` on
    the item in every Cart, Checkout, and Order line response whenever the
    effective identity differs from the default identity `("C62", 0)`.
  """
  if not isinstance(line, dict):
    return [
      f"{where}: expected a JSON object, got {type(line).__name__} "
      f"(CHECKOUT-0015)"
    ]
  item = line.get("item")
  if not isinstance(item, dict):
    return [
      f"{where}.item: expected a JSON object, got {item!r} (CHECKOUT-0015)"
    ]
  qu = item.get("quantity_unit")
  if not isinstance(qu, dict):
    return [
      f"{where}.item.quantity_unit: MUST be included whenever the effective "
      f"identity differs from the default ('C62', 0), got {qu!r} "
      f"(CHECKOUT-0015)"
    ]

  problems: list[str] = []
  identity = quantity_unit_machine_identity(qu)
  if identity is None:
    problems.append(
      f"{where}.item.quantity_unit: invalid unit or scale (expected non-empty "
      f"unit string and integer scale in 0..15), got {qu!r} (CHECKOUT-0015)"
    )
  else:
    unit, scale = identity
    if unit == "C62" and scale != 0:
      problems.append(
        f"{where}.item.quantity_unit: unit 'C62' requires effective scale 0, "
        f"got scale={scale} (CHECKOUT-0015)"
      )
    if identity == ("C62", 0):
      problems.append(
        f"{where}.item.quantity_unit: expected non-default machine identity "
        f"differing from ('C62', 0) for measured item, got {qu!r} "
        f"(CHECKOUT-0015)"
      )

  display_text = qu.get("display_text")
  if not isinstance(display_text, str) or not display_text.strip():
    problems.append(
      f"{where}.item.quantity_unit.display_text: must be a non-empty string, "
      f"got {display_text!r} (CHECKOUT-0015)"
    )

  return problems


def quantity_unit_comparison_problems(
  before_line: Any,
  matching_checkout: Any,
  mismatch_checkout: Any,
  *,
  expected_matching_qty: int = 2,
  where: str = "checkout",
) -> list[str]:
  """Return problems for `CHECKOUT-0003` and `CHECKOUT-0005` on unit assertions.

  Checks:
  - `REQ-SHOPPING-CHECKOUT-0003`: when the Platform includes `quantity_unit`,
    the Business MUST compare the asserted and authoritative machine identities
    `(unit, effective_scale)` (a `display_text` difference is not a mismatch).
  - `REQ-SHOPPING-CHECKOUT-0005`: if the identities cannot be reconciled, the
    Business MUST reject the request's effect on that line with a recoverable
    business outcome (`messages[]` entry with `type: "error"`,
    `severity: "recoverable"`, naming the authoritative basis), leaving the
    line at its previous authoritative state on an update.
  """
  if not isinstance(before_line, dict):
    return [
      f"{where} initial line: expected a JSON object, got {before_line!r} "
      f"(CHECKOUT-0003)"
    ]
  before_item = before_line.get("item") or {}
  auth_identity = quantity_unit_machine_identity(
    before_item.get("quantity_unit") if isinstance(before_item, dict) else None
  )
  if auth_identity is None:
    return [
      f"{where} initial line: malformed authoritative quantity_unit "
      f"{before_item.get('quantity_unit')!r} (CHECKOUT-0003)"
    ]

  problems: list[str] = []

  # 1. Matching machine identity (even with different display_text) MUST match.
  if not isinstance(matching_checkout, dict):
    problems.append(
      f"{where} on matching quantity_unit update: expected JSON object, got "
      f"{type(matching_checkout).__name__} (CHECKOUT-0003)"
    )
  else:
    m_lines = matching_checkout.get("line_items") or []
    if not isinstance(m_lines, list) or not m_lines:
      problems.append(
        f"{where} on matching quantity_unit update: line_items is empty "
        f"(CHECKOUT-0003)"
      )
    else:
      m_line = m_lines[0] if isinstance(m_lines[0], dict) else {}
      if m_line.get("quantity") != expected_matching_qty:
        problems.append(
          f"{where} on matching quantity_unit update: expected quantity "
          f"{expected_matching_qty} when asserted machine identity "
          f"{auth_identity!r} matches authoritative basis, got "
          f"{m_line.get('quantity')!r} (CHECKOUT-0003)"
        )
    m_errors = [
      m
      for m in (matching_checkout.get("messages") or [])
      if isinstance(m, dict) and m.get("type") == "error"
    ]
    if m_errors:
      problems.append(
        f"{where} on matching quantity_unit update: unexpected error messages "
        f"for matching machine identity {auth_identity!r} with alternate "
        f"display_text: {m_errors!r} (CHECKOUT-0003)"
      )

  # 2. Incompatible machine identity MUST be rejected with recoverable error
  #    and leave the line at its previous authoritative state.
  if not isinstance(mismatch_checkout, dict):
    problems.append(
      f"{where} on mismatched quantity_unit update: expected JSON object, got "
      f"{type(mismatch_checkout).__name__} (CHECKOUT-0003, CHECKOUT-0005)"
    )
    return problems

  mm_lines = mismatch_checkout.get("line_items") or []
  if not isinstance(mm_lines, list) or not mm_lines:
    problems.append(
      f"{where} on mismatched quantity_unit update: line MUST remain at its "
      f"previous authoritative state on an update, but line_items is empty "
      f"(CHECKOUT-0005)"
    )
  else:
    mm_line = mm_lines[0] if isinstance(mm_lines[0], dict) else {}
    if mm_line.get("quantity") != expected_matching_qty:
      problems.append(
        f"{where} on mismatched quantity_unit update: Business MUST reject the "
        f"request's effect on the line and leave quantity at "
        f"{expected_matching_qty}, got {mm_line.get('quantity')!r} "
        f"(CHECKOUT-0003, CHECKOUT-0005)"
      )
    mm_item = mm_line.get("item") if isinstance(mm_line, dict) else None
    mm_identity = quantity_unit_machine_identity(
      mm_item.get("quantity_unit") if isinstance(mm_item, dict) else None
    )
    if mm_identity != auth_identity:
      problems.append(
        f"{where} on mismatched quantity_unit update: authoritative "
        f"quantity_unit identity changed from {auth_identity!r} to "
        f"{mm_identity!r} (CHECKOUT-0005)"
      )

  messages = mismatch_checkout.get("messages")
  if not isinstance(messages, list) or not messages:
    problems.append(
      f"{where} on mismatched quantity_unit update: expected recoverable error "
      f"in messages[] naming the authoritative basis, got {messages!r} "
      f"(CHECKOUT-0005)"
    )
    return problems

  recoverable_errors = [
    m
    for m in messages
    if isinstance(m, dict)
    and m.get("type") == "error"
    and m.get("severity") == "recoverable"
    and isinstance(m.get("content"), str)
    and bool(m.get("content", "").strip())
  ]
  if not recoverable_errors:
    problems.append(
      f"{where} on mismatched quantity_unit update: expected at least one "
      f"messages[] entry with type=='error', severity=='recoverable', and "
      f"non-empty content, got {messages!r} (CHECKOUT-0005)"
    )

  return problems


def line_pricing_basis_unit_price_problems(
  line: Any, currency: Any, where: str = "line_item"
) -> list[str]:
  """Return problems for `CHECKOUT-0019` on a line with distinct pricing basis.

  Checks:
  - `REQ-SHOPPING-CHECKOUT-0019`: on authoritative responses the Business MUST
    include `item.unit_price` on every cart, checkout, and order line whose
    pricing basis differs from its sale basis.
  """
  if not isinstance(line, dict):
    return [
      f"{where}: expected a JSON object, got {type(line).__name__} "
      f"(CHECKOUT-0019)"
    ]
  item = line.get("item")
  if not isinstance(item, dict):
    return [
      f"{where}.item: expected a JSON object, got {item!r} (CHECKOUT-0019)"
    ]
  unit_price = item.get("unit_price")
  if not isinstance(unit_price, dict):
    return [
      f"{where}.item.unit_price: MUST be included when pricing basis differs "
      f"from sale basis, got {unit_price!r} (CHECKOUT-0019)"
    ]

  problems = [
    f"{e} (CHECKOUT-0019)"
    for e in unit_price_problems(
      unit_price,
      {"amount": item.get("price"), "currency": currency},
      where=f"{where}.item.unit_price",
    )
  ]

  sale_identity = quantity_unit_machine_identity(item.get("quantity_unit"))
  pricing_identity = quantity_unit_machine_identity(unit_price.get("measure"))
  if (
    sale_identity is not None
    and pricing_identity is not None
    and pricing_identity == sale_identity
  ):
    problems.append(
      f"{where}.item.unit_price: expected pricing basis {pricing_identity!r} "
      f"to differ from sale basis {sale_identity!r} for unit_price_item_sku "
      f"(CHECKOUT-0019)"
    )

  return problems


def order_measurement_settlement_problems(
  order_body: Any, where: str = "order"
) -> list[str]:
  """Return problems for `ORDER-0006` and `ORDER-0007` on settlement.

  Checks:
  - `REQ-SHOPPING-ORDER-0006`: when the price settles against an actual
    measurement, the adjustment MUST carry the settled `measure`.
  - `REQ-SHOPPING-ORDER-0007`: its unit identity MUST match the pricing basis
    (no conversion), and a pure price settlement uses `quantity: 0` so the
    count is untouched.
  """
  if not isinstance(order_body, dict):
    return [
      f"{where}: expected a JSON object, got {type(order_body).__name__} "
      f"(ORDER-0006, ORDER-0007)"
    ]

  order_lines = order_body.get("line_items")
  if not isinstance(order_lines, list) or not order_lines:
    return [
      f"{where}.line_items: expected non-empty list, got {order_lines!r} "
      f"(ORDER-0006, ORDER-0007)"
    ]
  lines_by_id = {
    li.get("id"): li
    for li in order_lines
    if isinstance(li, dict) and isinstance(li.get("id"), str)
  }

  adjustments = order_body.get("adjustments")
  if not isinstance(adjustments, list) or not adjustments:
    return [
      f"{where}.adjustments: expected at least one measurement settlement "
      f"adjustment, got {adjustments!r} (ORDER-0006, ORDER-0007)"
    ]

  problems: list[str] = []
  settled_entries: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
  for adj_idx, adj in enumerate(adjustments):
    if not isinstance(adj, dict):
      continue
    adj_lines = adj.get("line_items")
    if not isinstance(adj_lines, list):
      continue
    for li_idx, adj_li in enumerate(adj_lines):
      if isinstance(adj_li, dict) and adj_li.get("measure") is not None:
        settled_entries.append((adj_idx, li_idx, adj, adj_li))

  if not settled_entries:
    problems.append(
      f"{where}.adjustments: no adjustment line_items[] entry carries a "
      f"settled 'measure' when settling against an actual measurement, got "
      f"{adjustments!r} (ORDER-0006)"
    )
    return problems

  for adj_idx, li_idx, adj, adj_li in settled_entries:
    prefix = f"{where}.adjustments[{adj_idx}].line_items[{li_idx}]"
    measure = adj_li.get("measure")
    if not isinstance(measure, dict):
      problems.append(
        f"{prefix}.measure: expected a JSON object, got {measure!r} "
        f"(ORDER-0006)"
      )
      continue

    m_val = measure.get("value")
    if not _is_int(m_val) or m_val < 0:
      problems.append(
        f"{prefix}.measure.value: must be a non-negative integer, got "
        f"{m_val!r} (ORDER-0006)"
      )
    m_display = measure.get("display_text")
    if not isinstance(m_display, str) or not m_display.strip():
      problems.append(
        f"{prefix}.measure.display_text: must be a non-empty string, got "
        f"{m_display!r} (ORDER-0006)"
      )
    settled_identity = quantity_unit_machine_identity(measure)
    if settled_identity is None:
      problems.append(
        f"{prefix}.measure: invalid unit or scale in {measure!r} "
        f"(ORDER-0006, ORDER-0007)"
      )

    line_id = adj_li.get("id")
    order_li = lines_by_id.get(line_id)
    if not isinstance(order_li, dict):
      problems.append(
        f"{prefix}.id: references unknown order line {line_id!r} (ORDER-0007)"
      )
      continue

    item = order_li.get("item") or {}
    unit_price = item.get("unit_price") if isinstance(item, dict) else None
    up_measure = (
      unit_price.get("measure") if isinstance(unit_price, dict) else None
    )
    pricing_identity = (
      quantity_unit_machine_identity(up_measure)
      if isinstance(up_measure, dict)
      else None
    )
    if pricing_identity is None:
      problems.append(
        f"{where}.line_items[id={line_id!r}].item.unit_price.measure: "
        f"missing or invalid pricing basis for measurement settlement, got "
        f"{unit_price!r} (ORDER-0007)"
      )
    elif settled_identity is not None and settled_identity != pricing_identity:
      problems.append(
        f"{prefix}.measure: unit identity {settled_identity!r} MUST match "
        f"the line's pricing basis {pricing_identity!r} (no conversion) "
        f"(ORDER-0007)"
      )

    qty_obj = order_li.get("quantity")
    is_pure_price_settlement = adj.get("type") == "price_adjustment" or (
      isinstance(qty_obj, dict)
      and qty_obj.get("original") is not None
      and qty_obj.get("total") == qty_obj.get("original")
    )
    if is_pure_price_settlement:
      if adj_li.get("quantity") != 0:
        problems.append(
          f"{prefix}.quantity: pure price settlement MUST use quantity: 0 so "
          f"the count is untouched, got {adj_li.get('quantity')!r} "
          f"(ORDER-0007)"
        )
      if (
        isinstance(qty_obj, dict)
        and qty_obj.get("original") is not None
        and qty_obj.get("total") != qty_obj.get("original")
      ):
        problems.append(
          f"{where}.line_items[id={line_id!r}].quantity: pure price settlement "
          f"MUST leave count untouched (total == original), got {qty_obj!r} "
          f"(ORDER-0007)"
        )

  return problems


# ---------------------------------------------------------------------------
# W13: Buyer consent (draft model) (REQ-SHOPPING-BUYER-CONSENT-0003, -0004,
# -0010)
# ---------------------------------------------------------------------------


def _is_advertised_consent_purpose(entry: Any) -> bool:
  """Return whether `entry` is a valid advertised `ConsentPurpose` dict."""
  if not isinstance(entry, dict):
    return False
  granted = entry.get("granted")
  source = entry.get("source")
  description = entry.get("description")
  return (
    isinstance(granted, bool)
    and source in ("business", "platform")
    and isinstance(description, str)
    and bool(description.strip())
  )


def build_consent_confirmation(
  advertised_consent: Any,
  *,
  grant_purpose: str | None = None,
  grant_segment: str | None = None,
  unadvertised_purpose: str | None = None,
  unadvertised_segment: str | None = None,
) -> dict[str, Any]:
  """Build a platform `buyer.consent` confirmation map from advertised consent.

  Per `extensions/buyer-consent.md` (Confirm semantics), a confirm map echoes
  every advertised purpose and segment key with `granted` and `source` (omitting
  response-only `description` and `links`), optionally setting `grant_purpose` /
  `grant_segment` to `granted: True, source: "platform"` or injecting
  `unadvertised_purpose` / `unadvertised_segment` for negative tests.
  """
  confirmed: dict[str, Any] = {}
  first_advertised_purpose: str | None = None
  if isinstance(advertised_consent, dict):
    for p_key, p_val in advertised_consent.items():
      if not _is_reverse_domain_claim(p_key) or not isinstance(p_val, dict):
        continue
      if first_advertised_purpose is None:
        first_advertised_purpose = p_key
      p_entry: dict[str, Any] = {
        "granted": bool(p_val.get("granted", False)),
        "source": (
          p_val["source"]
          if p_val.get("source") in ("business", "platform")
          else "business"
        ),
      }
      raw_segments = p_val.get("segments")
      if isinstance(raw_segments, dict) and raw_segments:
        segs: dict[str, Any] = {}
        for s_key, s_val in raw_segments.items():
          if not _is_reverse_domain_claim(s_key) or not isinstance(s_val, dict):
            continue
          segs[s_key] = {
            "granted": bool(s_val.get("granted", False)),
            "source": (
              s_val["source"]
              if s_val.get("source") in ("business", "platform")
              else "business"
            ),
          }
        if segs:
          p_entry["segments"] = segs
      confirmed[p_key] = p_entry

  if grant_purpose is not None:
    p_entry = dict(
      confirmed.get(grant_purpose, {"granted": False, "source": "business"})
    )
    if grant_segment is None:
      p_entry["granted"] = True
      p_entry["source"] = "platform"
    else:
      segs = dict(p_entry.get("segments") or {})
      segs[grant_segment] = {"granted": True, "source": "platform"}
      p_entry["segments"] = segs
    confirmed[grant_purpose] = p_entry
    if first_advertised_purpose is None:
      first_advertised_purpose = grant_purpose

  if unadvertised_segment is not None:
    target_purpose = first_advertised_purpose or unadvertised_purpose
    if target_purpose is not None:
      p_entry = dict(
        confirmed.get(target_purpose, {"granted": True, "source": "platform"})
      )
      segs = dict(p_entry.get("segments") or {})
      segs[unadvertised_segment] = {"granted": True, "source": "platform"}
      p_entry["segments"] = segs
      confirmed[target_purpose] = p_entry

  if unadvertised_purpose is not None and unadvertised_purpose not in confirmed:
    confirmed[unadvertised_purpose] = {"granted": True, "source": "platform"}

  return confirmed


def unadvertised_consent_ignored_problems(
  before_checkout: Any,
  after_checkout: Any,
  unadvertised_purpose: str = "org.ucp.conformance.unadvertised_purpose",
  unadvertised_segment: str = "org.ucp.conformance.unadvertised_segment",
  where: str = "update response",
) -> list[str]:
  """Return every violation of `REQ-SHOPPING-BUYER-CONSENT-0010`.

  Checks `extensions/buyer-consent.md` (Normative requirements):
    - REQ-SHOPPING-BUYER-CONSENT-0010: Businesses MUST ignore purposes and
      segments in a request that were not advertised in a prior response.
  """
  if not isinstance(before_checkout, dict):
    return [f"{where}: before_checkout is not a dict ({before_checkout!r})"]
  if not isinstance(after_checkout, dict):
    return [f"{where}: after_checkout is not a dict ({after_checkout!r})"]

  before_buyer = before_checkout.get("buyer")
  before_consent = (
    before_buyer.get("consent") if isinstance(before_buyer, dict) else None
  )
  advertised_purposes: dict[str, set[str]] = {}
  if isinstance(before_consent, dict):
    for p_key, p_val in before_consent.items():
      if _is_reverse_domain_claim(p_key) and _is_advertised_consent_purpose(
        p_val
      ):
        raw_segs = p_val.get("segments")
        seg_keys = (
          {
            s_key
            for s_key, s_val in raw_segs.items()
            if _is_reverse_domain_claim(s_key)
            and _is_advertised_consent_purpose(s_val)
          }
          if isinstance(raw_segs, dict)
          else set()
        )
        advertised_purposes[p_key] = seg_keys

  problems: list[str] = []
  if not advertised_purposes:
    problems.append(
      f"{where}: prior checkout response did not advertise any reverse-DNS "
      f"consent purposes in buyer.consent, got {before_consent!r} "
      "(BUYER-CONSENT-0010)"
    )

  after_buyer = after_checkout.get("buyer")
  after_consent = (
    after_buyer.get("consent") if isinstance(after_buyer, dict) else None
  )
  if not isinstance(after_consent, dict):
    problems.append(
      f"{where}.buyer.consent: expected a consent map dict, got "
      f"{after_consent!r} (BUYER-CONSENT-0010)"
    )
    return problems

  if unadvertised_purpose in after_consent:
    problems.append(
      f"{where}.buyer.consent[{unadvertised_purpose!r}]: business MUST ignore "
      "unadvertised consent purpose, but echoed it in response "
      "(BUYER-CONSENT-0010)"
    )

  for p_key, p_val in after_consent.items():
    if advertised_purposes and p_key not in advertised_purposes:
      if p_key != unadvertised_purpose:
        problems.append(
          f"{where}.buyer.consent[{p_key!r}]: purpose was not advertised in "
          "prior response (BUYER-CONSENT-0010)"
        )
      continue
    if not isinstance(p_val, dict):
      problems.append(
        f"{where}.buyer.consent[{p_key!r}]: expected ConsentPurpose dict, "
        f"got {p_val!r} (BUYER-CONSENT-0010)"
      )
      continue
    raw_segs = p_val.get("segments")
    if isinstance(raw_segs, dict):
      allowed_segs = advertised_purposes.get(p_key, set())
      if unadvertised_segment in raw_segs:
        problems.append(
          f"{where}.buyer.consent[{p_key!r}].segments["
          f"{unadvertised_segment!r}]: business MUST ignore unadvertised "
          "consent segment, but echoed it in response (BUYER-CONSENT-0010)"
        )
      for s_key in raw_segs:
        if s_key != unadvertised_segment and s_key not in allowed_segs:
          problems.append(
            f"{where}.buyer.consent[{p_key!r}].segments[{s_key!r}]: segment "
            "was not advertised in prior response (BUYER-CONSENT-0010)"
          )

  return problems


def consent_unmet_dependency_completion_problems(
  complete_resp: Any,
  after_checkout: Any | None = None,
  where: str = "complete_checkout",
) -> list[str]:
  """Return every violation of `REQ-SHOPPING-BUYER-CONSENT-0003` and `-0004`.

  Checks `extensions/buyer-consent.md` (Data dependencies):
    - REQ-SHOPPING-BUYER-CONSENT-0003: On `complete_checkout`, businesses MUST
      NOT transition the checkout to `completed` while a confirmed consent
      decision has unmet data dependencies.
    - REQ-SHOPPING-BUYER-CONSENT-0004: Missing dependencies MUST be surfaced via
      the standard Error Handling flow.
  """
  if hasattr(complete_resp, "status_code") and hasattr(complete_resp, "json"):
    try:
      body = complete_resp.json()
    except ValueError as exc:
      return [
        f"{where}: response (HTTP {complete_resp.status_code}) is not valid "
        f"JSON: {exc} (BUYER-CONSENT-0004)"
      ]
  elif isinstance(complete_resp, dict):
    body = complete_resp
  else:
    return [f"{where}: response is not a JSON object ({complete_resp!r})"]

  if not isinstance(body, dict):
    return [f"{where}: response body is not a JSON object ({body!r})"]

  problems: list[str] = []
  if body.get("status") == "completed":
    problems.append(
      f"{where}.status: checkout MUST NOT transition to 'completed' while a "
      "confirmed consent decision has unmet data dependencies "
      "(BUYER-CONSENT-0003)"
    )
  if body.get("order") is not None:
    problems.append(
      f"{where}.order: checkout with unmet consent data dependencies MUST NOT "
      f"produce a completed order, got {body.get('order')!r} "
      "(BUYER-CONSENT-0003)"
    )

  if isinstance(after_checkout, dict) and (
    after_checkout.get("status") == "completed"
  ):
    problems.append(
      f"{where} (subsequent GET): checkout transitioned to 'completed' "
      "despite unmet consent data dependencies (BUYER-CONSENT-0003)"
    )

  messages = body.get("messages")
  if not isinstance(messages, list) or not messages:
    problems.append(
      f"{where}.messages: missing dependencies MUST be surfaced via the "
      f"standard Error Handling flow (messages[]), got {messages!r} "
      "(BUYER-CONSENT-0004)"
    )
    return problems

  error_msgs = [
    m
    for m in messages
    if isinstance(m, dict)
    and m.get("type") == "error"
    and isinstance(m.get("code"), str)
    and m["code"].strip()
    and isinstance(m.get("content"), str)
    and m["content"].strip()
  ]
  if not error_msgs:
    problems.append(
      f"{where}.messages: expected at least one error Message with non-empty "
      f"'code' and 'content' surfacing the missing consent dependency, got "
      f"{messages!r} (BUYER-CONSENT-0004)"
    )

  return problems


# ---------------------------------------------------------------------------
# W14: Order edits (REQ-SHOPPING-ORDER-0001)
# ---------------------------------------------------------------------------


def order_line_items_retained_after_edit_problems(
  checkout_body: Any,
  order_body: Any,
  where: str = "order",
) -> list[str]:
  """Check that an edited order retains all historical line items.

  Per `order/index.md` (Line Items, Status Derivation):
  - `order.line_items` **MUST** include all line items that ever existed on the
    order regardless of edits or alterations (`REQ-SHOPPING-ORDER-0001`).
  - `quantity.total` (`>= 0`) represents the current state while
    `quantity.original` (`> 0`) preserves the quantity from order creation.
  - When a line item's current quantity is reduced to `total == 0`, the line
    item remains in `order.line_items[]` with `status == "removed"`.
  """
  if not isinstance(checkout_body, dict):
    return [f"{where}: checkout_body is not a JSON object (ORDER-0001)"]
  if not isinstance(order_body, dict):
    return [f"{where}: order_body is not a JSON object (ORDER-0001)"]

  checkout_lines = checkout_body.get("line_items")
  if not isinstance(checkout_lines, list) or not checkout_lines:
    return [
      f"{where}: originating checkout has no line_items: {checkout_lines!r} "
      "(ORDER-0001)"
    ]

  order_lines = order_body.get("line_items")
  if not isinstance(order_lines, list) or not order_lines:
    return [
      f"{where}.line_items: expected non-empty list, got {order_lines!r} "
      "(ORDER-0001)"
    ]

  problems: list[str] = []
  order_lines_by_id: dict[str, dict[str, Any]] = {}
  order_lines_by_sku: dict[str, list[dict[str, Any]]] = {}
  has_quantity_edit = False

  for idx, oli in enumerate(order_lines):
    prefix = f"{where}.line_items[{idx}]"
    if not isinstance(oli, dict):
      problems.append(f"{prefix}: expected JSON object (ORDER-0001)")
      continue
    lid = oli.get("id")
    if isinstance(lid, str) and lid:
      order_lines_by_id[lid] = oli
    item = oli.get("item")
    sku = item.get("id") if isinstance(item, dict) else None
    if isinstance(sku, str) and sku:
      order_lines_by_sku.setdefault(sku, []).append(oli)

    qty = oli.get("quantity")
    if not isinstance(qty, dict):
      problems.append(
        f"{prefix}.quantity: expected OrderQuantityCounts object, got {qty!r} "
        "(ORDER-0001)"
      )
      continue
    total = qty.get("total")
    original = qty.get("original")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
      problems.append(
        f"{prefix}.quantity.total: expected integer >= 0, got {total!r} "
        "(ORDER-0001)"
      )
    if (
      not isinstance(original, int)
      or isinstance(original, bool)
      or original < 1
    ):
      problems.append(
        f"{prefix}.quantity.original: expected integer >= 1 preserving "
        f"original quantity, got {original!r} (ORDER-0001)"
      )
    if (
      isinstance(total, int)
      and not isinstance(total, bool)
      and isinstance(original, int)
      and not isinstance(original, bool)
    ):
      if total != original:
        has_quantity_edit = True
      if total == 0 and oli.get("status") != "removed":
        problems.append(
          f"{prefix}.status: line item edited to quantity.total == 0 MUST "
          f"remain on the order with status 'removed', got "
          f"{oli.get('status')!r} (ORDER-0001)"
        )

  for idx, cli in enumerate(checkout_lines):
    if not isinstance(cli, dict):
      continue
    c_id = cli.get("id")
    c_item = cli.get("item")
    c_sku = c_item.get("id") if isinstance(c_item, dict) else None
    c_qty = cli.get("quantity")

    matched = (
      order_lines_by_id.get(c_id)
      if isinstance(c_id, str) and c_id in order_lines_by_id
      else None
    )
    if matched is None and isinstance(c_sku, str):
      candidates = order_lines_by_sku.get(c_sku) or []
      if candidates:
        matched = candidates[0]

    if matched is None:
      problems.append(
        f"{where}.line_items: originating line item {idx} "
        f"(id={c_id!r}, item.id={c_sku!r}) was omitted after edit; "
        "order.line_items MUST include all line items that ever existed on "
        "the order regardless of edits or alterations (ORDER-0001)"
      )
      continue

    m_item = matched.get("item")
    m_sku = m_item.get("id") if isinstance(m_item, dict) else None
    if isinstance(c_sku, str) and m_sku != c_sku:
      problems.append(
        f"{where}.line_items[id={matched.get('id')!r}].item.id: expected "
        f"{c_sku!r} from originating checkout line, got {m_sku!r} (ORDER-0001)"
      )

    m_qty = matched.get("quantity")
    if (
      isinstance(c_qty, int)
      and not isinstance(c_qty, bool)
      and isinstance(m_qty, dict)
      and m_qty.get("original") != c_qty
    ):
      problems.append(
        f"{where}.line_items[id={matched.get('id')!r}].quantity.original: "
        f"expected {c_qty} from order creation, got "
        f"{m_qty.get('original')!r} (ORDER-0001)"
      )

  has_added_line = len(order_lines) > len(checkout_lines)
  adjustments = order_body.get("adjustments")
  has_edit_adjustment = isinstance(adjustments, list) and len(adjustments) > 0
  if not (has_quantity_edit or has_added_line or has_edit_adjustment):
    problems.append(
      f"{where}: order for edited_order_item_sku shows no edit or alteration "
      "(all line items have quantity.total == quantity.original, no line item "
      "was added, and adjustments[] is empty) (ORDER-0001)"
    )

  return problems


# ---------------------------------------------------------------------------
# W5 (Sub-decision 1A): Embedded binding delegation in REST Cart & Checkout
# responses (REQ-SHOPPING-CART-0030, REQ-SHOPPING-CHECKOUT-0152)
# ---------------------------------------------------------------------------


def embedded_entry(entries: Any) -> dict[str, Any] | None:
  """Return the embedded (`transport == 'embedded'`) service entry, if any."""
  items = entries if isinstance(entries, list) else [entries]
  return next(
    (
      item
      for item in items
      if isinstance(item, dict) and item.get("transport") == "embedded"
    ),
    None,
  )


def advertises_embedded_shopping(profile_or_body: Any) -> bool:
  """Return whether a `UcpProfile` or UCP JSON dict advertises `embedded`."""
  if profile_or_body is None:
    return False
  services = getattr(profile_or_body, "services", None)
  if isinstance(services, dict):
    return embedded_entry(services.get("dev.ucp.shopping", [])) is not None
  if isinstance(profile_or_body, dict):
    ucp_section = (
      profile_or_body.get("ucp")
      if isinstance(profile_or_body.get("ucp"), dict)
      else profile_or_body
    )
    svc_map = ucp_section.get("services")
    if isinstance(svc_map, dict):
      return embedded_entry(svc_map.get("dev.ucp.shopping", [])) is not None
  return False


def embedded_service_binding_problems(
  response_body: Any,
  *,
  rule_id: str = "CHECKOUT-0152",
  where: str = "response",
) -> list[str]:
  """Check that a Cart/Checkout response carries an embedded `config.delegate`.

  Per `cart/embedded.md` (`REQ-SHOPPING-CART-0030`) and `checkout/embedded.md`
  (`REQ-SHOPPING-CHECKOUT-0152`):
  - Businesses **MUST** include an embedded service binding (`transport:
    "embedded"`) with `config.delegate` in cart/checkout responses to indicate
    ECaP/ECP availability and allowed delegations for a specific session.
  """
  if not isinstance(response_body, dict):
    return [f"{where}: response is not a JSON object ({rule_id})"]

  ucp_meta = response_body.get("ucp")
  if not isinstance(ucp_meta, dict):
    return [
      f"{where}.ucp: missing or not a JSON object: {ucp_meta!r} ({rule_id})"
    ]

  services = ucp_meta.get("services")
  if not isinstance(services, dict):
    return [
      f"{where}.ucp.services: expected object containing 'dev.ucp.shopping' "
      f"embedded binding, got {services!r} ({rule_id})"
    ]

  shopping_entries = services.get("dev.ucp.shopping")
  items = (
    shopping_entries
    if isinstance(shopping_entries, list)
    else ([shopping_entries] if isinstance(shopping_entries, dict) else [])
  )
  embedded_entries = [
    item
    for item in items
    if isinstance(item, dict) and item.get("transport") == "embedded"
  ]
  if not embedded_entries:
    return [
      f"{where}.ucp.services['dev.ucp.shopping']: missing embedded service "
      f"binding (transport='embedded') with config.delegate ({rule_id})"
    ]

  problems: list[str] = []
  for idx, entry in enumerate(embedded_entries):
    prefix = f"{where}.ucp.services['dev.ucp.shopping'][embedded:{idx}]"
    version = entry.get("version")
    if not isinstance(version, str) or not version.strip():
      problems.append(
        f"{prefix}.version: expected non-empty version string, got "
        f"{version!r} ({rule_id})"
      )
    config = entry.get("config")
    if not isinstance(config, dict):
      problems.append(
        f"{prefix}.config: expected object with 'delegate' array, got "
        f"{config!r} ({rule_id})"
      )
      continue
    delegate = config.get("delegate")
    if not isinstance(delegate, list) or not all(
      isinstance(d, str) and bool(d.strip()) for d in delegate
    ):
      problems.append(
        f"{prefix}.config.delegate: expected array of delegation strings, got "
        f"{delegate!r} ({rule_id})"
      )

  return problems


class UnifiedUpdate(CheckoutUpdateRequest):
  """Client-side unified update model to support extensions."""


DEFAULT_SHOPPING_FIXTURES = str(
  Path(__file__).resolve().parent / "fixtures" / "flower_shop"
)
# Used when --conformance_input / --fixture_config / --test_data_dir are not
# given. The flags themselves live in framework.base_test.
DEFAULT_CONFORMANCE_INPUT = str(
  Path(DEFAULT_SHOPPING_FIXTURES) / "conformance_input.json"
)
DEFAULT_FIXTURE_CONFIG = str(
  Path(DEFAULT_SHOPPING_FIXTURES) / "test_fixtures.json"
)

# Capabilities a shopping merchant is expected to declare when its
# conformance input does not override `required_capabilities`.
DEFAULT_REQUIRED_CAPABILITIES = [
  "dev.ucp.shopping.checkout",
  "dev.ucp.shopping.order",
  "dev.ucp.shopping.discount",
  "dev.ucp.shopping.fulfillment",
  "dev.ucp.shopping.buyer_consent",
]


class FixturePaths(TypedDict):
  """The fixture files the shopping tests use."""

  conformance_input: str
  fixture_config: str
  data_dir: str


def fixture_paths() -> FixturePaths:
  """Return the fixture files to use: those configured, else the defaults.

  Each value comes from a flag or its FLAGS_<name> environment variable
  (see flag_value); the flower shop's files are the fallback.
  """
  return FixturePaths(
    conformance_input=flag_value("conformance_input")
    or DEFAULT_CONFORMANCE_INPUT,
    fixture_config=flag_value("fixture_config") or DEFAULT_FIXTURE_CONFIG,
    data_dir=flag_value("test_data_dir") or DEFAULT_SHOPPING_FIXTURES,
  )


class ShoppingTestData:
  """Holder for loaded retail test data."""

  def __init__(self) -> None:
    """Initialize ShoppingTestData container."""
    self.payment_instruments: list[dict[str, Any]] = []
    self.addresses: list[dict[str, Any]] = []

  def load(self, data_dir: str | Path) -> None:
    """Load data from CSV files in the given directory."""
    pi_path = Path(data_dir) / "payment_instruments.csv"
    if pi_path.exists():
      with pi_path.open(encoding="utf-8") as f:
        self.payment_instruments = list(csv.DictReader(f))

    addr_path = Path(data_dir) / "addresses.csv"
    if addr_path.exists():
      with addr_path.open(encoding="utf-8") as f:
        self.addresses = list(csv.DictReader(f))


shopping_test_data = ShoppingTestData()


def get_valid_payment_payload(
  instrument_id: str = "instr_1", address_id: str = "addr_1"
) -> dict[str, Any]:
  """Return a valid payment payload using loaded test data."""
  instr_data = next(
    (
      pi
      for pi in shopping_test_data.payment_instruments
      if pi["id"] == instrument_id
    ),
    None,
  )
  if not instr_data:
    instr_data = {
      "id": "instr_1",
      "type": "card",
      "brand": "Visa",
      "last_digits": "1234",
      "token": "success_token",
      "handler_id": "mock_payment_handler",
    }

  addr_data = next(
    (a for a in shopping_test_data.addresses if a["id"] == address_id),
    None,
  )
  if not addr_data:
    addr_data = {
      "street_address": "123 Main St",
      "city": "Anytown",
      "state": "CA",
      "postal_code": "12345",
      "country": "US",
    }

  billing_address = {
    "street_address": addr_data.get("street_address"),
    "address_locality": addr_data.get("city"),
    "address_region": addr_data.get("state"),
    "address_country": addr_data.get("country"),
    "postal_code": addr_data.get("postal_code"),
  }

  payment_instrument = {
    "id": instr_data["id"],
    "handler_id": instr_data["handler_id"],
    "type": instr_data["type"],
    "display": {
      "brand": instr_data["brand"],
      "last_digits": instr_data["last_digits"],
    },
    "credential": {"type": "token", "token": instr_data["token"]},
    "billing_address": billing_address,
  }

  return {
    "payment": {"instruments": [payment_instrument]},
    "risk_signals": {},
  }


class AddressFixture(TypedDict, total=False):
  """A stored address fixture, keyed with response-schema field names."""

  street_address: str
  address_locality: str
  address_region: str
  postal_code: str
  address_country: str


class CustomerFixture(TypedDict, total=False):
  """A buyer the server under test knows, with any stored addresses."""

  full_name: str
  email: str
  addresses: list[AddressFixture]


class DynamicFulfillmentCaseFixture(TypedDict):
  """A destination and fulfillment option expected for dynamic options."""

  destination: AddressFixture
  expected_option_id: str


class DynamicFulfillmentFixture(TypedDict):
  """Dynamic fulfillment cases declared by the server under test."""

  domestic: DynamicFulfillmentCaseFixture
  international: DynamicFulfillmentCaseFixture


class CatalogOptionSelectionFixture(TypedDict):
  """A single option selection (`name`, `label`) for a catalog product."""

  name: str
  label: str


class CatalogProductWithOptionsFixture(TypedDict):
  """A catalog product with configurable options and a sample selection."""

  id: str
  selected: list[CatalogOptionSelectionFixture]


class CatalogVariantFixture(TypedDict):
  """A catalog product ID and one of its variant IDs."""

  product_id: str
  variant_id: str


class CatalogFulfillsToFixture(TypedDict):
  """A catalog item, `fulfills_to` filter, and conflicting `location`."""

  item_id: str
  fulfills_to: dict[str, Any]
  conflicting_location: str


class ConsentWithDependencyFixture(TypedDict, total=False):
  """An advertised consent purpose/segment and the buyer field it requires."""

  purpose: str
  segment: str
  missing_buyer_field: str


class DynamicFixtureContext:
  """Context for loading dynamic test fixtures from configuration.

  The keys a server may leave out of `test_fixtures` are its *triggers*:
  each names a situation a test needs the server to be able to produce, such
  as a known customer with stored addresses. `TRIGGERS` lists them with the
  getter that reads each; a getter returns None when the key is absent or
  malformed, and `ShoppingIntegrationTestBase.require_trigger` turns that
  into a skip that names the key. The keys are described in
  `shopping/fixtures/README.md`.
  """

  # Each trigger key and the getter that reads it, in the README's order.
  TRIGGERS = {
    "valid_discount_code_2": "get_test_discount_code_2",
    "valid_fixed_discount_code": "get_test_fixed_discount_code",
    "known_customer": "get_known_customer",
    "known_customer_without_address": "get_known_customer_without_address",
    "free_shipping_min_subtotal": "get_free_shipping_min_subtotal",
    "free_shipping_item_sku": "get_free_shipping_item_sku",
    "dynamic_fulfillment": "get_dynamic_fulfillment",
    "catalog_product_with_options": "get_catalog_product_with_options",
    "catalog_variant": "get_catalog_variant",
    "unit_price_item_sku": "get_unit_price_item_sku",
    "catalog_search_default_page_size": "get_catalog_search_default_page_size",
    "accepted_eligibility_claim": "get_accepted_eligibility_claim",
    "pricing_eligibility_claim": "get_pricing_eligibility_claim",
    "unverifiable_eligibility_claim": "get_unverifiable_eligibility_claim",
    "unserviceable_destination_id": "get_unserviceable_destination_id",
    "pickup_location_id": "get_pickup_location_id",
    "catalog_fulfills_to": "get_catalog_fulfills_to",
    "custom_total_item_sku": "get_custom_total_item_sku",
    "total_lines_item_sku": "get_total_lines_item_sku",
    "disclosure_item_sku": "get_disclosure_item_sku",
    "async_completion_item_sku": "get_async_completion_item_sku",
    "escalation_item_sku": "get_escalation_item_sku",
    "action_item_sku": "get_action_item_sku",
    "measured_item_sku": "get_measured_item_sku",
    "settlement_item_sku": "get_settlement_item_sku",
    "consent_with_dependency": "get_consent_with_dependency",
    "edited_order_item_sku": "get_edited_order_item_sku",
  }

  def __init__(
    self,
    config_path: str | Path | dict[str, Any],
    fallback_config: dict[str, Any] | None = None,
  ):
    """Initialize DynamicFixtureContext with path or dictionary."""
    if isinstance(config_path, (str, Path)):
      try:
        with Path(config_path).open(encoding="utf-8") as f:
          self._config = json.load(f)
      except (FileNotFoundError, OSError):
        self._config = {}
    elif isinstance(config_path, dict):
      self._config = config_path
    else:
      self._config = {}
    self._fallback_config = fallback_config or {}

  def trigger(self, name: str) -> Any:
    """Return what the server declares for a trigger, or None if nothing.

    Args:
      name: A key of `TRIGGERS`.

    Returns:
      The key's value as its getter reads it, or None when the server
      declares no such key, or a malformed one.

    Raises:
      KeyError: If the name isn't a trigger. That is a mistake in a test,
        not a gap in the fixtures, so it isn't a skip.

    """
    if name not in self.TRIGGERS:
      raise KeyError(
        f"{name!r} is not a trigger; see DynamicFixtureContext.TRIGGERS"
      )
    return getattr(self, self.TRIGGERS[name])()

  def get_test_sku(self) -> str:
    """Get the test SKU or item ID to use in checkout tests."""
    val = self._config.get("test_sku")
    if val is None:
      val = self._fallback_config.get("test_sku")
    if val is not None:
      return str(val)

    val = self._config.get("test_fixtures", {}).get("valid_item", {}).get("sku")
    if val is None:
      val = (
        self._fallback_config.get("test_fixtures", {})
        .get("valid_item", {})
        .get("sku")
      )
    if val is not None:
      return str(val)

    items = self._config.get("items", [{}])
    if not items or items == [{}]:
      items = self._fallback_config.get("items", [{}])
    if items and isinstance(items, list) and len(items) > 0:
      return str(items[0].get("id", "item_1"))
    return "item_1"

  def get_test_price(self) -> int:
    """Get the expected price for the valid item in minor units."""
    val = self._config.get("test_price")
    if val is None:
      val = self._fallback_config.get("test_price")
    if val is not None:
      if isinstance(val, (int, float)):
        return int(round(val * 100))
      return int(val)

    val = (
      self._config.get("test_fixtures", {})
      .get("valid_item", {})
      .get("expected_price")
    )
    if val is None:
      val = (
        self._fallback_config.get("test_fixtures", {})
        .get("valid_item", {})
        .get("expected_price")
      )
    if val is not None:
      if isinstance(val, (int, float)):
        return int(round(val * 100))
      return int(val)

    items = self._config.get("items", [{}])
    if not items or items == [{}]:
      items = self._fallback_config.get("items", [{}])
    if items and isinstance(items, list) and len(items) > 0:
      return int(items[0].get("price", 3500))
    return 3500

  def get_test_destination(self) -> dict[str, Any]:
    """Get the destination address dictionary for shipping."""
    val = self._config.get("test_destination")
    if val is None:
      val = self._fallback_config.get("test_destination")

    if val is not None and isinstance(val, dict):
      dest = dict(val)
    else:
      dest = self._config.get("shipping_locations", {}).get(
        "domestic_destination", {}
      )
      if not dest:
        dest = self._fallback_config.get("shipping_locations", {}).get(
          "domestic_destination", {}
        )
      dest = dict(dest) if dest else {}

    if not dest:
      dest = {
        "street": "123 Market St",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94105",
        "country": "US",
      }
    dest.setdefault("address_country", dest.get("country", "US"))
    dest.setdefault("postal_code", dest.get("postal_code", "94105"))
    dest.setdefault("locality", dest.get("city", "San Francisco"))
    dest.setdefault("region", dest.get("state", "CA"))
    dest.setdefault("street_address", dest.get("street", "123 Market St"))
    return dest

  def get_test_discount_code(self) -> str:
    """Get a valid discount code for tests."""
    val = self._config.get("test_discount_code")
    if val is None:
      val = self._fallback_config.get("test_discount_code")
    if val is not None:
      return str(val)

    val = self._config.get("test_fixtures", {}).get("valid_discount_code")
    if val is None:
      val = self._fallback_config.get("test_fixtures", {}).get(
        "valid_discount_code"
      )
    if val is not None:
      return str(val)
    return "SPRING20"

  def get_test_discount_code_2(self) -> str | None:
    """Get a second valid discount code for tests."""
    val = self._config.get("test_discount_code_2")
    if val is None:
      val = self._fallback_config.get("test_discount_code_2")
    if val is not None:
      return str(val)

    val = self._config.get("test_fixtures", {}).get("valid_discount_code_2")
    if val is None:
      val = self._fallback_config.get("test_fixtures", {}).get(
        "valid_discount_code_2"
      )
    if val is not None:
      return str(val)
    return None

  def get_test_fixed_discount_code(self) -> str | None:
    """Get a valid fixed discount code for tests."""
    val = self._config.get("test_fixed_discount_code")
    if val is None:
      val = self._fallback_config.get("test_fixed_discount_code")
    if val is not None:
      return str(val)

    val = self._config.get("test_fixtures", {}).get("valid_fixed_discount_code")
    if val is None:
      val = self._fallback_config.get("test_fixtures", {}).get(
        "valid_fixed_discount_code"
      )
    if val is not None:
      return str(val)
    return None

  def get_expected_discount_percentage(self, default=0.0) -> float:
    """Get expected discount percentage (e.g. 10.0 for 10%)."""
    val = self._config.get("test_fixtures", {}).get(
      "expected_discount_percentage"
    )
    if val is None:
      val = self._fallback_config.get("test_fixtures", {}).get(
        "expected_discount_percentage"
      )
    if val is not None:
      return float(val)
    return float(default)

  def get_expected_discount_percentage_2(self, default=0.0) -> float:
    """Get expected discount percentage 2."""
    val = self._config.get("test_fixtures", {}).get(
      "expected_discount_percentage_2"
    )
    if val is None:
      val = self._fallback_config.get("test_fixtures", {}).get(
        "expected_discount_percentage_2"
      )
    if val is not None:
      return float(val)
    return float(default)

  def get_expected_fixed_discount_reduction(self, default=0.0) -> int:
    """Get expected fixed discount reduction in minor units."""
    val = self._config.get("test_fixtures", {}).get(
      "expected_fixed_discount_reduction"
    )
    if val is None:
      val = self._fallback_config.get("test_fixtures", {}).get(
        "expected_fixed_discount_reduction"
      )
    if val is not None:
      return int(round(float(val) * 100))
    return int(round(default * 100))

  def _get_test_fixture(self, key: str) -> Any:
    """Look up a key under test_fixtures in config, then fallback config."""
    val = self._config.get("test_fixtures", {}).get(key)
    if val is None:
      val = self._fallback_config.get("test_fixtures", {}).get(key)
    return val

  def get_known_customer(self) -> CustomerFixture | None:
    """Get a buyer the server knows and stores addresses for, if configured.

    Returns None when the server under test declares no such customer, in
    which case the stored-address tests skip.
    """
    val = self._get_test_fixture("known_customer")
    if isinstance(val, dict) and val.get("email"):
      return CustomerFixture(val)
    return None

  def get_known_customer_without_address(self) -> CustomerFixture | None:
    """Get a buyer the server knows but has no stored addresses for."""
    val = self._get_test_fixture("known_customer_without_address")
    if isinstance(val, dict) and val.get("email"):
      return CustomerFixture(val)
    return None

  def get_free_shipping_min_subtotal(self) -> int | None:
    """Get the subtotal threshold for free shipping in minor units.

    Returns None when the server under test declares no order-value
    free-shipping promotion, in which case the threshold test skips.
    """
    val = self._get_test_fixture("free_shipping_min_subtotal")
    if val is None:
      return None
    return int(round(float(val) * 100))

  def get_free_shipping_item_sku(self) -> str | None:
    """Get the SKU of an item eligible for item-based free shipping."""
    val = self._get_test_fixture("free_shipping_item_sku")
    if val is None:
      return None
    return str(val)

  def get_dynamic_fulfillment(self) -> DynamicFulfillmentFixture | None:
    """Get destination/option pairs for dynamic fulfillment tests.

    Returns None when the server under test declares no dynamic fulfillment
    cases, in which case the dynamic-options test skips. Each case declares a
    destination address (response-schema field names) and the fulfillment
    option id the server returns for that destination.
    """
    val = self._get_test_fixture("dynamic_fulfillment")
    if not isinstance(val, dict):
      return None
    if not isinstance(val.get("domestic"), dict) or not isinstance(
      val.get("international"), dict
    ):
      return None
    domestic = val["domestic"]
    international = val["international"]
    if not isinstance(domestic.get("destination"), dict) or not isinstance(
      domestic.get("expected_option_id"), str
    ):
      return None
    if not isinstance(international.get("destination"), dict) or not isinstance(
      international.get("expected_option_id"), str
    ):
      return None
    return DynamicFulfillmentFixture(
      domestic=domestic, international=international
    )

  def get_catalog_product_with_options(
    self,
  ) -> CatalogProductWithOptionsFixture | None:
    """Get a catalog product with options and a sample selection."""
    val = self._get_test_fixture("catalog_product_with_options")
    if not isinstance(val, dict):
      return None
    product_id = val.get("id")
    selected = val.get("selected")
    if not isinstance(product_id, str) or not product_id.strip():
      return None
    if not isinstance(selected, list) or not selected:
      return None
    for entry in selected:
      if not isinstance(entry, dict):
        return None
      if not isinstance(entry.get("name"), str) or not entry["name"].strip():
        return None
      if not isinstance(entry.get("label"), str) or not entry["label"].strip():
        return None
    return CatalogProductWithOptionsFixture(id=product_id, selected=selected)

  def get_catalog_variant(self) -> CatalogVariantFixture | None:
    """Get a product ID and one of its variant IDs."""
    val = self._get_test_fixture("catalog_variant")
    if not isinstance(val, dict):
      return None
    product_id = val.get("product_id")
    variant_id = val.get("variant_id")
    if not isinstance(product_id, str) or not product_id.strip():
      return None
    if not isinstance(variant_id, str) or not variant_id.strip():
      return None
    return CatalogVariantFixture(product_id=product_id, variant_id=variant_id)

  def get_unit_price_item_sku(self) -> str | None:
    """Get the SKU or variant ID of an item with `unit_price`."""
    val = self._get_test_fixture("unit_price_item_sku")
    if not isinstance(val, str) or not val.strip():
      return None
    return val

  def get_catalog_search_default_page_size(self) -> int | None:
    """Get the default page size for `/catalog/search` when omitting `limit`."""
    val = self._get_test_fixture("catalog_search_default_page_size")
    if not _is_int(val) or val < 1:
      return None
    return val

  def get_accepted_eligibility_claim(self) -> str | None:
    """Get a reverse-domain `context.eligibility` claim the server accepts."""
    val = self._get_test_fixture("accepted_eligibility_claim")
    return val if _is_reverse_domain_claim(val) else None

  def get_pricing_eligibility_claim(self) -> str | None:
    """Get a reverse-domain claim that triggers a provisional discount."""
    val = self._get_test_fixture("pricing_eligibility_claim")
    return val if _is_reverse_domain_claim(val) else None

  def get_unverifiable_eligibility_claim(self) -> str | None:
    """Get an accepted `context.eligibility` claim that fails verification."""
    val = self._get_test_fixture("unverifiable_eligibility_claim")
    return val if _is_reverse_domain_claim(val) else None

  def get_unserviceable_destination_id(self) -> str | None:
    """Get a recognized destination ID that fails revalidation in Checkout."""
    val = self._get_test_fixture("unserviceable_destination_id")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_pickup_location_id(self) -> str | None:
    """Get a Business Location ID advertised for `pickup`."""
    val = self._get_test_fixture("pickup_location_id")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_catalog_fulfills_to(self) -> CatalogFulfillsToFixture | None:
    """Get a catalog item, `fulfills_to` filter, and conflicting location."""
    val = self._get_test_fixture("catalog_fulfills_to")
    if not isinstance(val, dict):
      return None
    item_id = val.get("item_id")
    fulfills_to = val.get("fulfills_to")
    conflicting = val.get("conflicting_location")
    if not isinstance(item_id, str) or not item_id.strip():
      return None
    if not isinstance(fulfills_to, dict) or not fulfills_to:
      return None
    if not isinstance(conflicting, str) or not conflicting.strip():
      return None
    return CatalogFulfillsToFixture(
      item_id=item_id,
      fulfills_to=fulfills_to,
      conflicting_location=conflicting,
    )

  def get_custom_total_item_sku(self) -> str | None:
    """Get an item SKU whose checkout `totals[]` includes a custom type."""
    val = self._get_test_fixture("custom_total_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_total_lines_item_sku(self) -> str | None:
    """Get an item SKU whose checkout `totals[]` includes `lines[]`."""
    val = self._get_test_fixture("total_lines_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_disclosure_item_sku(self) -> str | None:
    """Get an item SKU that triggers a disclosure warning in Checkout."""
    val = self._get_test_fixture("disclosure_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_async_completion_item_sku(self) -> str | None:
    """Get an item SKU whose checkout completes asynchronously."""
    val = self._get_test_fixture("async_completion_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_escalation_item_sku(self) -> str | None:
    """Get an item SKU whose checkout returns `status: requires_escalation`."""
    val = self._get_test_fixture("escalation_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_action_item_sku(self) -> str | None:
    """Get an item SKU that surfaces an outstanding Action."""
    val = self._get_test_fixture("action_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_measured_item_sku(self) -> str | None:
    """Get an item SKU whose sale basis differs from the default ('C62', 0)."""
    val = self._get_test_fixture("measured_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_settlement_item_sku(self) -> str | None:
    """Get an item SKU whose order carries a measurement settlement."""
    val = self._get_test_fixture("settlement_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_consent_with_dependency(
    self,
  ) -> ConsentWithDependencyFixture | None:
    """Get a consent purpose/segment with a buyer data dependency."""
    val = self._get_test_fixture("consent_with_dependency")
    if not isinstance(val, dict):
      return None
    purpose = val.get("purpose")
    missing_field = val.get("missing_buyer_field")
    if (
      not _is_reverse_domain_claim(purpose)
      or not isinstance(missing_field, str)
      or not missing_field.strip()
    ):
      return None
    segment = val.get("segment")
    if segment is not None and not _is_reverse_domain_claim(segment):
      return None
    result = ConsentWithDependencyFixture(
      purpose=purpose,
      missing_buyer_field=missing_field.strip(),
    )
    if segment is not None:
      result["segment"] = segment
    return result

  def get_edited_order_item_sku(self) -> str | None:
    """Get an item SKU whose completed order undergoes a post-order edit."""
    val = self._get_test_fixture("edited_order_item_sku")
    if isinstance(val, str) and val.strip():
      return val
    return None

  def get_non_existent_id(self) -> str:
    """Get an identifier the server under test does not sell."""
    for source in (self._config, self._fallback_config):
      entry = source.get("non_existent_item")
      if isinstance(entry, dict) and entry.get("id"):
        return str(entry["id"])
    return "non_existent_id_99999"

  def get_catalog_max_batch_size(self, default: int = 1000) -> int:
    """Get the maximum batch size for `/catalog/lookup`, defaulting to 1000."""
    val = self._get_test_fixture("catalog_max_batch_size")
    if _is_int(val) and val >= 1:
      return val
    return default

  def get_catalog_search_query(self) -> str:
    """Get a search query string that matches items in the server's catalog."""
    val = self._get_test_fixture("catalog_search_query")
    if isinstance(val, str) and val.strip():
      return val
    return self.get_test_sku()

  def get_unrecognized_eligibility_claim(
    self, default: str = "org.ucp.conformance.unrecognized_claim"
  ) -> str:
    """Get a reverse-domain `context.eligibility` claim the server ignores."""
    val = self._get_test_fixture("unrecognized_eligibility_claim")
    if _is_reverse_domain_claim(val):
      return val
    return default


ConfiguredFixtureContext = DynamicFixtureContext


def rest_entry(entries: Any) -> dict[str, Any] | None:
  """Return the REST entry of a service's discovery entries, if any.

  A profile lists a service once per transport, and the REST entry, the
  binding these tests use, need not come first.
  """
  items = entries if isinstance(entries, list) else [entries]
  return next(
    (
      item
      for item in items
      if isinstance(item, dict) and item.get("transport") == "rest"
    ),
    None,
  )


def shopping_rest_endpoint(profile: dict[str, Any]) -> str:
  """Return the REST endpoint of the shopping service in a profile.

  Raises:
    RuntimeError: The profile lists no shopping service, or no REST entry
      with an endpoint for it.

  """
  ucp_data = profile.get("ucp", profile)
  shopping_services = ucp_data.get("services", {}).get("dev.ucp.shopping", [])
  if not shopping_services:
    raise RuntimeError("Shopping service not found in discovery profile")
  endpoint = (rest_entry(shopping_services) or {}).get("endpoint")
  if not endpoint:
    raise RuntimeError(
      "Shopping service endpoint not found in discovery profile"
    )
  return str(endpoint)


# Every shopping test needs the checkout capability; declaring it here makes
# BaseIntegrationTest skip before it starts the agent profile server.
@requires_capability("dev.ucp.shopping.checkout")
class ShoppingIntegrationTestBase(BaseIntegrationTest):
  """Base integration test class for all UCP Retail Shopping tests."""

  default_conformance_input = DEFAULT_CONFORMANCE_INPUT

  def setUp(self) -> None:
    """Set up shopping fixtures, configs, and endpoints."""
    super().setUp()

    paths = fixture_paths()

    # Load fixture configuration
    fixture_config = {}
    fix_cfg_path = paths["fixture_config"]
    if Path(fix_cfg_path).exists():
      with Path(fix_cfg_path).open(encoding="utf-8") as f:
        fixture_config = json.load(f)
    else:
      logging.warning("Fixture config not found at %s", fix_cfg_path)

    self.fixture_ctx = DynamicFixtureContext(
      fixture_config, fallback_config=self.conformance_config
    )

    # Load CSV test data
    fixtures_dir = paths["data_dir"]
    if Path(fixtures_dir).exists():
      try:
        shopping_test_data.load(fixtures_dir)
      except Exception as e:  # pylint: disable=broad-exception-caught
        logging.warning("Failed to load shopping CSV fixtures: %s", e)
    else:
      logging.warning("Test data directory not found at %s", fixtures_dir)

    self._shopping_service_endpoint: str | None = None

  @property
  def shopping_service_endpoint(self) -> str:
    """Cached property for the shopping service endpoint."""
    if self._shopping_service_endpoint is None:
      discovery_resp = self.client.get(self.discovery_url)
      self.assert_response_status(discovery_resp, 200)
      self._shopping_service_endpoint = shopping_rest_endpoint(
        discovery_resp.json()
      )
    return self._shopping_service_endpoint

  def get_shopping_url(self, path: str) -> str:
    """Construct a full URL for the shopping service."""
    base = self.shopping_service_endpoint.rstrip("/")
    path = path.lstrip("/")
    return f"{base}/{path}"

  def get_order_url(self, order_id: str) -> str:
    """Construct a full URL for an order resource."""
    return self.get_shopping_url(f"/orders/{order_id}")

  def require_trigger(self, name: str) -> Any:
    """Return what the fixtures declare for a trigger, or skip the test.

    The skip reads "no trigger declared: <name>", which names the key of
    `test_fixtures` to add (see `shopping/fixtures/README.md`). A test that
    needs more of a declared trigger, such as a known customer with two
    stored addresses, checks that itself and skips with `skip_no_trigger`,
    naming the key and the shape it needs.

    Args:
      name: A key of `DynamicFixtureContext.TRIGGERS`.

    Returns:
      The key's value, as its getter reads it.

    """
    value = self.fixture_ctx.trigger(name)
    if value is None:
      self.skip_no_trigger(name)
    return value

  def assert_line_totals(self, checkout_data: dict[str, Any]) -> None:
    """Assert every line's total is `price × quantity × 10^-scale`.

    checkout.md (Pricing a line) has the Business compute the line total so
    and round once at the line. Each line is checked against its own
    response values: the item's price, the quantity, and the scale of the
    `quantity_unit` it echoes (0 when absent). With scale 0 the product is
    exact; with a measured line, a single rounding leaves the total within
    one minor unit of the product, which is what is asserted.

    Args:
      checkout_data: A checkout as the server returned it.

    """
    lines = checkout_data.get("line_items") or []
    self.assertTrue(lines, "the checkout has no line items to check")
    for index, line in enumerate(lines):
      exact, scale = expected_line_total(line)
      actual = line_total_entry(line)
      where = f"line_items[{index}] ({line.get('item', {}).get('id')!r})"
      self.assertIsNotNone(
        actual,
        f"{where} carries no subtotal entry in totals[]: {line.get('totals')}",
      )
      if scale == 0:
        self.assertEqual(
          actual,
          int(exact),
          f"{where}: total {actual} is not price × quantity "
          f"({line['item']['price']} × {line['quantity']} = {int(exact)}) "
          "(checkout.md: the line total is price × quantity × 10^-scale)",
        )
      else:
        self.assertLess(
          abs(actual - exact),
          1,
          f"{where}: total {actual} is not price × quantity × 10^-{scale} "
          f"({line['item']['price']} × {line['quantity']} × 10^-{scale} = "
          f"{exact}) rounded once at the line",
        )

  def assert_same_order_snapshot(
    self, order_id: str, actual: dict[str, Any], what: str
  ) -> dict[str, Any]:
    """Assert an order entity is the snapshot Get Order returns right now.

    order.md (Operations): the order entity is a current-state snapshot, the
    same for synchronous retrieval and event delivery, and the Business MUST
    return the full entity on every response. So a delivery, or the order a
    completion response carries, must match Get Order member for member.

    Args:
      order_id: The order's ID.
      actual: The order entity to check.
      what: What carried it, for the message, such as "the Order created
        delivery".

    Returns:
      The order as Get Order returned it.

    """
    response = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(response, 200)
    authoritative = response.json()
    differences = snapshot_differences(authoritative, actual)
    self.assertFalse(
      differences,
      f"{what} is not the full order entity Get Order returns "
      "(order.md: the full entity on every response, the same current-state "
      "snapshot for retrieval and delivery); differences:\n  "
      + "\n  ".join(differences),
    )
    return authoritative

  def assert_order_carries_checkout(
    self, order_data: dict[str, Any], checkout_data: dict[str, Any]
  ) -> None:
    """Assert an order entity carries the checkout it was placed from.

    order.md (Operations): the order is the current-state snapshot of the
    placed order, and the Business MUST return the full entity on every
    response. Right after placement that state is the completed checkout's,
    so the order must name it and carry its currency, each of its lines
    (the item and the quantity ordered) and its total; a reference, a delta
    or a trimmed snapshot does not.

    Args:
      order_data: The order as the server returned it.
      checkout_data: The completed checkout it was placed from.

    """
    self.assertEqual(
      order_data.get("checkout_id"),
      checkout_data["id"],
      "the order does not name the checkout it was placed from",
    )
    self.assertEqual(
      order_data.get("currency"),
      checkout_data["currency"],
      "the order's currency is not the checkout's",
    )
    ordered = {
      line["item"]["id"]: line for line in order_data.get("line_items") or []
    }
    for line in checkout_data["line_items"]:
      item_id = line["item"]["id"]
      self.assertIn(
        item_id,
        ordered,
        f"the order carries no line for the checkout's item {item_id!r}; "
        f"order lines: {sorted(ordered)}",
      )
      quantity = ordered[item_id].get("quantity")
      ordered_total = (
        quantity.get("total") if isinstance(quantity, dict) else quantity
      )
      self.assertEqual(
        ordered_total,
        line["quantity"],
        f"the order's quantity for {item_id!r} is not the checkout's",
      )
    totals = {
      entry.get("type"): entry.get("amount")
      for entry in order_data.get("totals") or []
    }
    expected = {
      entry.get("type"): entry.get("amount")
      for entry in checkout_data.get("totals") or []
    }
    self.assertEqual(
      totals.get("total"),
      expected.get("total"),
      "the order's total is not the completed checkout's",
    )

  def assert_totals_consistent(
    self, checkout_obj, expected_subtotal, expected_discount=0
  ):
    """Assert that the checkout totals are consistent and correct.

    The grand total is verified with the spec's own formula (checkout.md
    "Verification"): sum of the signed amounts of every non-``total``
    entry equals the ``total`` entry's amount. Every entry participates:
    all types except ``subtotal``/``total`` MAY repeat (checkout.md
    "Repeating Types" — e.g. multi-jurisdiction tax lines or itemized
    fees) and ``type`` is an open string, so picking one entry per
    well-known type would both reject conformant servers and miss
    servers whose total drops a repeated or custom entry.
    """
    subtotal = next(
      (t.amount for t in checkout_obj.totals if t.type == "subtotal"), 0
    )
    discount = sum(
      t.amount
      for t in checkout_obj.totals
      if t.type in ["items_discount", "discount"]
    )
    total_entries = [t for t in checkout_obj.totals if t.type == "total"]
    self.assertLen(
      total_entries,
      1,
      "Exactly one totals entry of type 'total' must be present "
      "(checkout.md totals invariants), got "
      f"{[(t.type, t.amount) for t in checkout_obj.totals]}",
    )
    total = total_entries[0].amount

    self.assertEqual(
      subtotal,
      expected_subtotal,
      f"Subtotal mismatch: expected {expected_subtotal}, got {subtotal}",
    )
    if expected_discount:
      if isinstance(expected_discount, (list, set, tuple)):
        self.assertIn(
          abs(discount),
          expected_discount,
          (
            f"Discount mismatch: expected one of {expected_discount}, got"
            f" {discount}"
          ),
        )
      else:
        self.assertEqual(
          abs(discount),
          expected_discount,
          f"Discount mismatch: expected {expected_discount}, got {discount}",
        )
    # checkout.md "Verification":
    #   assert sum(e.amount for e in totals if e.type != "total") \
    #     == total_entry.amount
    # Amounts are signed integers; the sign IS the direction.
    calculated_total = sum(
      t.amount for t in checkout_obj.totals if t.type != "total"
    )
    self.assertEqual(
      total,
      calculated_total,
      (
        f"Total math mismatch: calculated {calculated_total} from entries "
        f"{[(t.type, t.amount) for t in checkout_obj.totals]}, got {total}"
      ),
    )

  def create_checkout_payload(
    self,
    quantity=1,
    item_id: str | None = None,
    currency: str | None = None,
    handlers=None,
    buyer: dict[str, Any] | None = None,
    include_fulfillment: bool = True,
    context: dict[str, Any] | None = None,
  ) -> checkout_create_request.CheckoutCreateRequest:
    """Create a valid checkout creation payload."""
    ctx = getattr(self, "fixture_ctx", None) or DynamicFixtureContext(
      getattr(self, "conformance_config", {})
    )

    if item_id is None:
      item_id = ctx.get_test_sku()
    if currency is None:
      currency = getattr(self, "conformance_config", {}).get("currency", "USD")

    if handlers is None:
      handlers = [
        payment_handler.Base(
          id="google_pay",
          name="google.pay",
          version=self.ucp_version,
          spec="https://example.com/spec",
          config_schema="https://example.com/schema",
          instrument_schemas=["https://example.com/instrument_schema"],
          config={},
        )
      ]

    item = item_create_request.ItemCreateRequest(id=item_id)
    line_item = line_item_create_request.LineItemCreateRequest(
      quantity=quantity, item=item
    )

    payment = payment_create_request.PaymentCreateRequest(
      instruments=[],
      handlers=[h.model_dump(mode="json", exclude_none=True) for h in handlers],
    )

    fulfillment = None
    if include_fulfillment:
      dest_data = ctx.get_test_destination()
      destination = (
        shipping_destination_create_request.ShippingDestinationCreateRequest(
          id="dest_1",
          type="shipping_address",
          address_country=dest_data.get(
            "address_country", dest_data.get("country", "US")
          ),
          postal_code=dest_data.get("postal_code", "94105"),
          locality=dest_data.get("locality", dest_data.get("city")),
          region=dest_data.get("region", dest_data.get("state")),
          street_address=dest_data.get(
            "street_address", dest_data.get("street")
          ),
        )
      )
      group = fulfillment_group_create_request.FulfillmentGroupCreateRequest(
        id="group_1",
        line_item_ids=["line_item_123"],
        selected_option_id="std-ship",
      )
      method = fulfillment_method_create_request.FulfillmentMethodCreateRequest(
        id="method_1",
        type="shipping",
        destinations=[destination],
        line_item_ids=["line_item_123"],
        selected_destination_id="dest_1",
        groups=[group],
      )
      fulfillment = {
        "methods": [
          method.model_dump(mode="json", exclude_none=True, by_alias=True)
        ]
      }

    item.price = ctx.get_test_price()
    line_item.id = "line_item_123"
    line_item.totals = []

    checkout_req = checkout_create_request.CheckoutCreateRequest(
      id=str(uuid.uuid4()),
      currency=currency,
      line_items=[line_item],
      payment=payment,
      buyer=buyer,
      fulfillment=fulfillment,
      context=context,
    )
    checkout_req.status = "incomplete"
    checkout_req.ucp = {"version": self.ucp_version}
    checkout_req.totals = []
    checkout_req.links = []

    return checkout_req

  def create_checkout_session(
    self,
    quantity: int = 1,
    item_id: str | None = None,
    currency: str | None = None,
    handlers: list[Any] | None = None,
    buyer: dict[str, Any] | None = None,
    select_fulfillment: bool = True,
    headers: dict[str, str] | None = None,
    context: dict[str, Any] | None = None,
  ) -> Any:
    """Create a checkout session and return the response JSON."""
    create_payload = self.create_checkout_payload(
      quantity=quantity,
      item_id=item_id,
      currency=currency,
      handlers=handlers,
      buyer=buyer,
      include_fulfillment=select_fulfillment,
      context=context,
    )

    request_headers = self.get_headers()
    if headers:
      request_headers.update(headers)

    payload_dict = create_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    if isinstance(buyer, dict) and "consent" in buyer:
      payload_dict.setdefault("buyer", {})["consent"] = buyer["consent"]

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=payload_dict,
      headers=request_headers,
    )
    self.assert_response_status(response, [200, 201])
    checkout_data = response.json()

    if select_fulfillment:
      checkout_data = self.ensure_fulfillment_ready(
        checkout_data["id"], context=context
      )

    return checkout_data

  def ensure_fulfillment_ready(
    self, checkout_id: str, context: dict[str, Any] | None = None
  ) -> Any:
    """Ensure a fulfillment option is selected for the checkout."""
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    checkout_data = response.json()

    def is_ready(data):
      if not data.get("fulfillment") or not data["fulfillment"].get("methods"):
        return False
      method = data["fulfillment"]["methods"][0]
      if not method.get("selected_destination_id"):
        return False
      return method.get("groups") and method["groups"][0].get(
        "selected_option_id"
      )

    if is_ready(checkout_data):
      return checkout_data

    checkout_obj = f_models.Checkout(**checkout_data)

    has_destinations = (
      checkout_data.get("fulfillment")
      and checkout_data["fulfillment"].get("methods")
      and checkout_data["fulfillment"]["methods"][0].get("destinations")
    )

    if not has_destinations:
      address = {
        "id": "dest_default",
        "street_address": "123 Default St",
        "address_locality": "City",
        "address_region": "State",
        "postal_code": "12345",
        "address_country": "US",
      }
      method_id = None
      if checkout_data.get("fulfillment") and checkout_data["fulfillment"].get(
        "methods"
      ):
        method_id = checkout_data["fulfillment"]["methods"][0].get("id")

      method_payload = {
        "type": "shipping",
        "destinations": [address],
        "selected_destination_id": "dest_default",
      }
      if method_id:
        method_payload["id"] = method_id

      checkout_data = self.update_checkout_session(
        checkout_obj,
        fulfillment={"methods": [method_payload]},
        context=context,
      )
      checkout_obj = f_models.Checkout(**checkout_data)

    method = checkout_data["fulfillment"]["methods"][0]
    if not method.get("selected_destination_id") and method.get("destinations"):
      dest_id = method["destinations"][0]["id"]
      method_payload = method.copy()
      method_payload["selected_destination_id"] = dest_id

      checkout_data = self.update_checkout_session(
        checkout_obj,
        fulfillment={"methods": [method_payload]},
        context=context,
      )
      checkout_obj = f_models.Checkout(**checkout_data)

    method = checkout_data["fulfillment"]["methods"][0]
    has_selection = False
    if method.get("groups"):
      for g in method["groups"]:
        if g.get("selected_option_id"):
          has_selection = True
          break

    if not has_selection and (
      method.get("groups") and method["groups"][0].get("options")
    ):
      option_id = method["groups"][0]["options"][0]["id"]
      method_payload = method.copy()
      method_payload["groups"][0]["selected_option_id"] = option_id

      checkout_data = self.update_checkout_session(
        checkout_obj,
        fulfillment={"methods": [method_payload]},
        context=context,
      )

    return checkout_data

  def attempt_complete_checkout(
    self,
    checkout_id: str,
    payment_payload: dict[str, Any] | None = None,
    buyer: dict[str, Any] | None = None,
  ) -> httpx.Response:
    """POST /checkout-sessions/{id}/complete and return the raw response."""
    if payment_payload is None:
      payment_payload = get_valid_payment_payload()
    if buyer is not None:
      payment_payload = {**payment_payload, "buyer": buyer}
    return self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=self.get_headers(),
    )

  def complete_checkout_session(
    self, checkout_id: str, payment_payload: dict[str, Any] | None = None
  ) -> Any:
    """Complete a checkout session."""
    self.ensure_fulfillment_ready(checkout_id)
    response = self.attempt_complete_checkout(checkout_id, payment_payload)
    self.assert_response_status(response, 200)
    return response.json()

  def create_completed_order(self) -> str:
    """Orchestrate checkout creation and completion."""
    checkout_data = self.create_checkout_session()
    checkout_id = checkout_data["id"]
    complete_data = self.complete_checkout_session(checkout_id)
    return complete_data["order"]["id"]

  def checkout_update_payload(
    self,
    checkout_obj: Any,
    currency: str | None = None,
    line_items: list[Any] | None = None,
    payment: Any | None = None,
    buyer: Any | None = None,
    fulfillment: Any | None = None,
    discounts: Any | None = None,
    platform: Any | None = None,
    context: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    """Build the body of an Update Checkout request, as JSON.

    PUT replaces the whole checkout, so every optional field that is not
    passed explicitly is carried over from `checkout_obj`; otherwise a partial
    update such as `update_checkout_session(obj, discounts=...)` would wipe
    the buyer and fulfillment state the test set up earlier.
    """
    currency = currency if currency is not None else checkout_obj.currency

    def to_json(obj):
      if obj is not None and hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
      return obj

    if buyer is None:
      buyer = to_json(getattr(checkout_obj, "buyer", None))
    if fulfillment is None:
      fulfillment = to_json(getattr(checkout_obj, "fulfillment", None))
    if discounts is None:
      discounts = to_json(getattr(checkout_obj, "discounts", None))
    if platform is None:
      platform = to_json(getattr(checkout_obj, "platform", None))
    if context is None:
      context = to_json(getattr(checkout_obj, "context", None))

    if line_items is None:
      line_items = []
      for li in checkout_obj.line_items:
        item_update = item_update_request.ItemUpdateRequest(
          id=li.item.id,
        )
        line_items.append(
          line_item_update_request.LineItemUpdateRequest(
            id=li.id,
            item=item_update,
            quantity=li.quantity,
            parent_id=li.parent_id,
          )
        )

    if payment is None:
      payment = (
        payment_update_request.PaymentUpdateRequest(
          instruments=getattr(checkout_obj.payment, "instruments", []),
        )
        if checkout_obj.payment
        else None
      )

    if isinstance(fulfillment, dict) and "methods" in fulfillment:
      for m in fulfillment["methods"]:
        if isinstance(m, dict) and "destinations" in m and m["destinations"]:
          for d in m["destinations"]:
            if isinstance(d, dict) and "type" not in d:
              d["type"] = "shipping_address"

    update_payload = UnifiedUpdate(
      id=checkout_obj.id,
      currency=currency,
      line_items=line_items,
      payment=payment,
      buyer=buyer,
      fulfillment=fulfillment,
      discounts=discounts,
      platform=platform,
    )
    dumped = update_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    if isinstance(buyer, dict) and "consent" in buyer:
      dumped.setdefault("buyer", {})["consent"] = buyer["consent"]
    if context is not None:
      dumped["context"] = context
    return dumped

  def update_checkout_session(
    self,
    checkout_obj: Any,
    currency: str | None = None,
    line_items: list[Any] | None = None,
    payment: Any | None = None,
    buyer: Any | None = None,
    fulfillment: Any | None = None,
    discounts: Any | None = None,
    platform: Any | None = None,
    headers: dict[str, str] | None = None,
    context: dict[str, Any] | None = None,
  ) -> Any:
    """Update a checkout session; return the response JSON.

    The body comes from `checkout_update_payload`, which carries over every
    field not passed explicitly, since PUT replaces the whole checkout.
    """
    request_headers = self.get_headers()
    if headers:
      request_headers.update(headers)

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      json=self.checkout_update_payload(
        checkout_obj,
        currency=currency,
        line_items=line_items,
        payment=payment,
        buyer=buyer,
        fulfillment=fulfillment,
        discounts=discounts,
        platform=platform,
        context=context,
      ),
      headers=request_headers,
    )
    self.assert_response_status(response, 200)
    return response.json()

  def assert_provisional_discount(
    self,
    checkout_data: Any,
    claim: str,
    where: str = "discounts.applied",
  ) -> None:
    """Assert `checkout_data` surfaces a provisional discount for `claim`."""
    problems = provisional_discount_problems(checkout_data, claim, where=where)
    self.assertFalse(
      problems,
      f"{where} violates provisional discount rules for {claim!r}:\n  "
      + "\n  ".join(problems),
    )

  def assert_eligibility_invalid_error(
    self, body: Any, where: str = "complete response"
  ) -> None:
    """Assert `body` rejects completion with `eligibility_invalid`."""
    problems = eligibility_invalid_error_problems(body, where=where)
    self.assertFalse(
      problems,
      f"{where} violates unverified eligibility completion rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_selected_destination(
    self,
    method: Any,
    expected_id: str | None = None,
    expected_type: str | None = None,
    where: str = "fulfillment.methods[0]",
  ) -> None:
    """Assert `method` satisfies accepted `selected_destination_id` rules."""
    problems = selected_destination_problems(
      method,
      expected_id=expected_id,
      expected_type=expected_type,
      where=where,
    )
    self.assertFalse(
      problems,
      f"{where} violates selected_destination_id rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_rejected_destination_update(
    self,
    before_checkout: Any,
    after_checkout: Any,
    method_index: int = 0,
    where: str = "update response",
  ) -> None:
    """Assert a rejected destination update leaves checkout state intact."""
    problems = rejected_destination_update_problems(
      before_checkout,
      after_checkout,
      method_index=method_index,
      where=where,
    )
    self.assertFalse(
      problems,
      f"{where} violates rejected selected_destination_id update rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_option_description(
    self, option: Any, where: str = "option"
  ) -> None:
    """Assert `option.description` does not repeat `title` or `total`."""
    problems = option_description_problems(option, where=where)
    self.assertFalse(
      problems,
      f"{where} violates fulfillment option description rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_unknown_total_types(
    self, totals: Any, where: str = "totals"
  ) -> None:
    """Assert unknown `totals[].type` entries include `display_text`."""
    problems = unknown_total_type_problems(totals, where=where)
    self.assertFalse(
      problems,
      f"{where} violates unknown total type rules:\n  " + "\n  ".join(problems),
    )

  def assert_total_sub_lines(self, totals: Any, where: str = "totals") -> None:
    """Assert `sum(lines[].amount) == amount` on `totals[]` entries."""
    problems = total_sub_lines_problems(totals, where=where)
    self.assertFalse(
      problems,
      f"{where} violates totals[].lines[] rules:\n  " + "\n  ".join(problems),
    )

  def assert_rfc9535_jsonpath(
    self,
    path_value: Any,
    response_body: dict[str, Any] | None = None,
    where: str = "messages[].path",
  ) -> None:
    """Assert `path_value` is a valid RFC 9535 JSONPath relative to root."""
    problems = rfc9535_jsonpath_problems(path_value, response_body, where=where)
    self.assertFalse(
      problems,
      f"{where} violates RFC 9535 JSONPath rules:\n  " + "\n  ".join(problems),
    )

  def assert_absolute_https_url(
    self, url_value: Any, where: str = "continue_url"
  ) -> None:
    """Assert `url_value` is an absolute HTTPS URL."""
    problems = absolute_https_url_problems(url_value, where=where)
    self.assertFalse(
      problems,
      f"{where} violates absolute HTTPS URL rules:\n  " + "\n  ".join(problems),
    )

  def assert_disclosure_warning(
    self,
    body: Any,
    *,
    require_policy_link: bool = False,
    where: str = "checkout",
  ) -> None:
    """Assert `body` includes a compliant disclosure warning."""
    problems = disclosure_warning_problems(
      body, require_policy_link=require_policy_link, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates disclosure warning rules:\n  " + "\n  ".join(problems),
    )

  def assert_complete_in_progress(
    self, body: Any, where: str = "complete response"
  ) -> None:
    """Assert `body` has `status: complete_in_progress` and no `order`."""
    problems = complete_in_progress_problems(body, where=where)
    self.assertFalse(
      problems,
      f"{where} violates complete_in_progress rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_update_during_complete_in_progress(
    self,
    before_checkout: Any,
    after_checkout: Any,
    where: str = "update response",
  ) -> None:
    """Assert Update during `complete_in_progress` leaves checkout intact."""
    problems = update_during_complete_in_progress_problems(
      before_checkout, after_checkout, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates complete_in_progress update rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_requires_escalation(
    self,
    body: Any,
    *,
    check_continue_url: bool = True,
    check_buyer_message: bool = True,
    where: str = "checkout",
  ) -> None:
    """Assert `body` satisfies `requires_escalation` rules."""
    problems = requires_escalation_problems(
      body,
      check_continue_url=check_continue_url,
      check_buyer_message=check_buyer_message,
      where=where,
    )
    self.assertFalse(
      problems,
      f"{where} violates requires_escalation rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_outstanding_actions(
    self, body: Any, where: str = "entity"
  ) -> None:
    """Assert `body` surfaces at least one outstanding Action in `actions`."""
    problems = outstanding_actions_problems(body, where=where)
    self.assertFalse(
      problems,
      f"{where} violates outstanding Actions rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_cart_action_non_blocking(
    self,
    before_cart: Any,
    after_cart: Any,
    *,
    expected_bag: list[tuple[Any, Any]] | None = None,
    where: str = "cart update response",
  ) -> None:
    """Assert an outstanding Action does not block an unrelated cart update."""
    problems = cart_action_non_blocking_problems(
      before_cart, after_cart, expected_bag=expected_bag, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates non-blocking Cart Action rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_action_surfaced_before_complete(
    self,
    pre_complete_checkout: Any,
    complete_response_checkout: Any = None,
    where: str = "checkout",
  ) -> None:
    """Assert Actions requiring input were surfaced before Complete Checkout."""
    problems = action_surfaced_before_complete_problems(
      pre_complete_checkout, complete_response_checkout, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates pre-Complete Action surfacing rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_action_prevents_complete(
    self,
    before_checkout: Any,
    complete_body: Any,
    where: str = "complete response",
  ) -> None:
    """Assert Complete blocked by an Action returns `incomplete` + `path`."""
    problems = action_prevents_complete_problems(
      before_checkout, complete_body, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates Action-blocked Complete Checkout rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_non_default_quantity_unit(
    self, line: Any, where: str = "line_item"
  ) -> None:
    """Assert `line` includes a non-default `item.quantity_unit`."""
    problems = non_default_quantity_unit_problems(line, where=where)
    self.assertFalse(
      problems,
      f"{where} violates non-default quantity_unit rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_quantity_unit_comparison(
    self,
    before_line: Any,
    matching_checkout: Any,
    mismatch_checkout: Any,
    *,
    expected_matching_qty: int = 2,
    where: str = "checkout",
  ) -> None:
    """Assert `quantity_unit` machine-identity comparison and rejection."""
    problems = quantity_unit_comparison_problems(
      before_line,
      matching_checkout,
      mismatch_checkout,
      expected_matching_qty=expected_matching_qty,
      where=where,
    )
    self.assertFalse(
      problems,
      f"{where} violates quantity_unit machine-identity rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_line_pricing_basis_unit_price(
    self, line: Any, currency: Any, where: str = "line_item"
  ) -> None:
    """Assert `line` includes `item.unit_price` when pricing basis differs."""
    problems = line_pricing_basis_unit_price_problems(
      line, currency, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates line pricing-basis unit_price rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_order_measurement_settlement(
    self, order_body: Any, where: str = "order"
  ) -> None:
    """Assert `order_body` records a valid measurement settlement adjustment."""
    problems = order_measurement_settlement_problems(order_body, where=where)
    self.assertFalse(
      problems,
      f"{where} violates measurement settlement adjustment rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_unadvertised_consent_ignored(
    self,
    before_checkout: Any,
    after_checkout: Any,
    unadvertised_purpose: str = "org.ucp.conformance.unadvertised_purpose",
    unadvertised_segment: str = "org.ucp.conformance.unadvertised_segment",
    where: str = "update response",
  ) -> None:
    """Assert unadvertised consent purposes and segments are ignored."""
    problems = unadvertised_consent_ignored_problems(
      before_checkout,
      after_checkout,
      unadvertised_purpose=unadvertised_purpose,
      unadvertised_segment=unadvertised_segment,
      where=where,
    )
    self.assertFalse(
      problems,
      f"{where} violates authoritative advertised consent rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_consent_unmet_dependency_completion(
    self,
    complete_resp: Any,
    after_checkout: Any | None = None,
    where: str = "complete_checkout",
  ) -> None:
    """Assert unmet consent data dependency prevents completion with error."""
    problems = consent_unmet_dependency_completion_problems(
      complete_resp, after_checkout=after_checkout, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates consent data-dependency completion rules:\n  "
      + "\n  ".join(problems),
    )

  def assert_order_line_items_retained_after_edit(
    self,
    checkout_body: Any,
    order_body: Any,
    where: str = "order",
  ) -> None:
    """Assert `order_body` retains all originating line items after an edit."""
    problems = order_line_items_retained_after_edit_problems(
      checkout_body, order_body, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates order line item retention rules:\n  "
      + "\n  ".join(problems),
    )

  def require_embedded_shopping(self) -> None:
    """Skip if the server does not advertise the embedded shopping transport."""
    if self.server_profile is not None and not advertises_embedded_shopping(
      self.server_profile
    ):
      self.skipTest(
        skip_reason(CAPABILITY, "dev.ucp.shopping (embedded transport)")
      )

  def assert_embedded_service_binding(
    self,
    response_body: Any,
    *,
    rule_id: str = "CHECKOUT-0152",
    where: str = "response",
  ) -> None:
    """Assert `response_body` carries an embedded binding `config.delegate`."""
    problems = embedded_service_binding_problems(
      response_body, rule_id=rule_id, where=where
    )
    self.assertFalse(
      problems,
      f"{where} violates embedded service binding rules:\n  "
      + "\n  ".join(problems),
    )

  # ── cart capability (cart.md, cart/rest.md) ──────────────────────────────

  def create_cart(
    self,
    quantity: int = 1,
    item_id: str | None = None,
    buyer: dict[str, Any] | None = None,
    discounts: dict[str, Any] | None = None,
    **extra: Any,
  ) -> dict[str, Any]:
    """POST /carts with the fixtures' item; return the cart as created.

    Args:
      quantity: The quantity of the line.
      item_id: The item to put in the cart; the fixtures' SKU by default.
      buyer: An optional buyer object (cart.json: optional on create).
      discounts: An optional discounts object, for a cart extended with
        the discount extension.
      **extra: Further top-level request members.

    Returns:
      The cart entity, as the server returned it.

    """
    payload: dict[str, Any] = {
      "line_items": [
        {
          "item": {"id": item_id or self.fixture_ctx.get_test_sku()},
          "quantity": quantity,
        }
      ]
    }
    if buyer is not None:
      payload["buyer"] = buyer
    if discounts is not None:
      payload["discounts"] = discounts
    payload.update(extra)
    response = self.client.post(
      self.get_shopping_url("/carts"), json=payload, headers=self.get_headers()
    )
    self.assert_response_status(response, [200, 201])
    cart = response.json()
    self.assertTrue(cart.get("id"), f"the created cart has no id: {cart}")
    return cart

  def update_cart(
    self,
    cart_id: str,
    line_items: list[dict[str, Any]],
    **extra: Any,
  ) -> httpx.Response:
    """PUT /carts/{id} with full replacement payload; return raw response."""
    payload: dict[str, Any] = {"id": cart_id, "line_items": line_items}
    payload.update(extra)
    return self.client.put(
      self.get_shopping_url(f"/carts/{cart_id}"),
      json=payload,
      headers=self.get_headers(),
    )

  def get_cart(self, cart_id: str) -> httpx.Response:
    """GET /carts/{id}; return the raw response."""
    return self.client.get(
      self.get_shopping_url(f"/carts/{cart_id}"), headers=self.get_headers()
    )

  def cancel_cart(self, cart_id: str) -> httpx.Response:
    """POST /carts/{id}/cancel; return the raw response."""
    return self.client.post(
      self.get_shopping_url(f"/carts/{cart_id}/cancel"),
      headers=self.get_headers(),
    )

  def convert_cart(self, cart_id: str, **payload: Any) -> dict[str, Any]:
    """Create a checkout from a cart; return the checkout.

    cart.md (Cart-to-Checkout Conversion): the platform passes `cart_id` in
    the Create Checkout request, MUST include `line_items` and SHOULD set it
    to an empty array. Each call sends a fresh Idempotency-Key, so a second
    conversion is a new request, not a replay.

    Args:
      cart_id: The cart to convert.
      **payload: Further request members, such as the overlapping fields a
        test wants the business to ignore.

    Returns:
      The checkout entity, as the server returned it.

    """
    body: dict[str, Any] = {"cart_id": cart_id, "line_items": []}
    body.update(payload)
    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=body,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, [200, 201])
    checkout_data = response.json()
    self.assertTrue(
      checkout_data.get("id"),
      f"the checkout created from cart {cart_id} has no id: {checkout_data}",
    )
    return checkout_data

  @staticmethod
  def line_item_bag(entity: dict[str, Any]) -> list[tuple[Any, Any]]:
    """Return an entity's line items as a sorted bag of (item id, quantity).

    Carts and checkouts assign their own line IDs, so contents are compared
    by what was ordered, not by those IDs.
    """
    return sorted(
      ((line.get("item") or {}).get("id"), line.get("quantity"))
      for line in entity.get("line_items") or []
    )

  # ── catalog capabilities (catalog/lookup.md, catalog/search.md) ──────────

  def catalog_lookup(self, ids: list[str], **extra: Any) -> httpx.Response:
    """POST /catalog/lookup with `ids`; return the raw response."""
    payload: dict[str, Any] = {"ids": ids}
    payload.update(extra)
    return self.client.post(
      self.get_shopping_url("/catalog/lookup"),
      json=payload,
      headers=self.get_headers(),
    )

  def catalog_product(self, item_id: str, **extra: Any) -> httpx.Response:
    """POST /catalog/product with `id`; return the raw response."""
    payload: dict[str, Any] = {"id": item_id}
    payload.update(extra)
    return self.client.post(
      self.get_shopping_url("/catalog/product"),
      json=payload,
      headers=self.get_headers(),
    )

  def catalog_search(
    self, payload: dict[str, Any] | None = None, **extra: Any
  ) -> httpx.Response:
    """POST /catalog/search with `payload`; return the raw response."""
    body: dict[str, Any] = dict(payload or {})
    body.update(extra)
    return self.client.post(
      self.get_shopping_url("/catalog/search"),
      json=body,
      headers=self.get_headers(),
    )

  def assert_unit_price(
    self, unit_price: Any, price: Any, where: str = "unit_price"
  ) -> None:
    """Assert that a `unit_price` object satisfies CATALOG-0003..0009."""
    problems = unit_price_problems(unit_price, price, where=where)
    self.assertFalse(
      problems,
      f"{where} violates catalog unit_price invariants:\n  "
      + "\n  ".join(problems),
    )

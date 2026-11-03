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

"""Structural and location/selection conformance tests for fulfillment.md.

Covers the server-side `MUST` and `MUST NOT` rules of
`docs/specification/shopping/extensions/fulfillment.md` for:
- `options[].title` sibling distinction (`REQ-SHOPPING-FULFILLMENT-0018`)
- `options[].description` non-repetition of `title` or `total`
  (`REQ-SHOPPING-FULFILLMENT-0021`)
- Single consolidating group per method under default configuration
  (`REQ-SHOPPING-FULFILLMENT-0048`)
- Accepted `selected_destination_id` echoing and single self-describing
  `destinations[]` entry (`REQ-SHOPPING-FULFILLMENT-0012`,
  `REQ-SHOPPING-FULFILLMENT-0013`)
- Rejected `selected_destination_id` (unrecognized ID or failed revalidation)
  leaving Checkout unchanged and returning a recoverable error message on the
  attempted method's `selected_destination_id` path
  (`REQ-SHOPPING-FULFILLMENT-0014`, `REQ-SHOPPING-FULFILLMENT-0016`,
  `REQ-SHOPPING-FULFILLMENT-0017`)
- Location precedence (`selected_destination_id` over `context.location` in
  Checkout, `REQ-SHOPPING-FULFILLMENT-0002`; `filters.fulfills_to` over
  `context` in Catalog, `REQ-SHOPPING-FULFILLMENT-0001`)
- Business Location destination selection for `pickup`
  (`REQ-SHOPPING-FULFILLMENT-0009`, `REQ-SHOPPING-FULFILLMENT-0035`)
"""

from typing import Any
import uuid

from absl.testing import absltest
from framework.decorators import requires_capability, spec_assert
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas import payment_handler
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import checkout_create_request

try:
  from ucp_sdk.models.schemas.shopping import payment_create_request
except ImportError:
  from ucp_sdk.models.schemas.common.types import payment_create_request
from ucp_sdk.models.schemas.shopping.types import item_create_request
from ucp_sdk.models.schemas.shopping.types import line_item_create_request

_FULFILLMENT_CAPABILITY = "dev.ucp.shopping.fulfillment"
_CATALOG_LOOKUP_CAPABILITY = "dev.ucp.shopping.catalog.lookup"


def _methods(response_json: dict) -> list[dict]:
  """Return the fulfillment ``methods[]`` from a raw checkout response.

  Tolerates the SDK's ``RootModel`` wrapping (``fulfillment.root``) that a
  serializer may or may not flatten.
  """
  fulfillment = response_json.get("fulfillment") or {}
  if "methods" not in fulfillment and isinstance(fulfillment.get("root"), dict):
    fulfillment = fulfillment["root"]
  return fulfillment.get("methods") or []


def _option_has_description(option: dict[str, Any]) -> bool:
  """Return whether `option` carries a non-empty `description`."""
  raw = option.get("description")
  if isinstance(raw, str):
    return bool(raw.strip())
  if isinstance(raw, dict):
    return any(
      isinstance(raw.get(k), str) and raw[k].strip()
      for k in ("plain", "markdown")
    )
  return False


def _method_options_signature(
  method: dict[str, Any],
) -> list[tuple[Any, Any]]:
  """Return `(option_id, totals)` pairs across all groups on `method`."""
  sig: list[tuple[Any, Any]] = []
  for group in method.get("groups") or []:
    for opt in group.get("options") or []:
      if isinstance(opt, dict):
        sig.append((opt.get("id"), opt.get("totals")))
  return sig


def _variant_fulfillment_methods(
  lookup_json: dict[str, Any], item_id: str
) -> list[dict[str, Any]]:
  """Return `fulfillment.methods[]` for `item_id` from a catalog response."""
  products = lookup_json.get("products") or []
  target_variant: dict[str, Any] | None = None
  for prod in products:
    if not isinstance(prod, dict):
      continue
    variants = prod.get("variants") or []
    for var in variants:
      if isinstance(var, dict) and var.get("id") == item_id:
        target_variant = var
        break
    if target_variant is not None:
      break
    if prod.get("id") == item_id and variants and isinstance(variants[0], dict):
      target_variant = variants[0]
      break
  if target_variant is None and products and isinstance(products[0], dict):
    variants = products[0].get("variants") or []
    if variants and isinstance(variants[0], dict):
      target_variant = variants[0]
  if target_variant is None:
    return []
  fulfillment = target_variant.get("fulfillment") or {}
  methods = fulfillment.get("methods") if isinstance(fulfillment, dict) else []
  return methods if isinstance(methods, list) else []


@requires_capability(_FULFILLMENT_CAPABILITY)
class FulfillmentStructureTest(ShoppingIntegrationTestBase):
  """Structural and selection invariants of the fulfillment extension.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - POST /catalog/lookup
  """

  def _shipping_destination(self, dest_id: str = "dest_1") -> dict[str, Any]:
    """Build the default test shipping destination dict."""
    dest = self.fixture_ctx.get_test_destination()
    return {
      "id": dest_id,
      "street_address": dest.get(
        "street_address", dest.get("street", "123 Market St")
      ),
      "address_locality": dest.get(
        "locality", dest.get("city", "San Francisco")
      ),
      "address_region": dest.get("region", dest.get("state", "CA")),
      "postal_code": dest.get("postal_code", "94105"),
      "address_country": dest.get("address_country", dest.get("country", "US")),
    }

  def _shipping_fulfillment_payload(
    self,
    line_item_ids: list[str],
    selected_destination_id: str = "dest_1",
    destinations: list[dict[str, Any]] | None = None,
  ) -> dict[str, Any]:
    """Build a single-method shipping fulfillment payload."""
    if destinations is None:
      destinations = [self._shipping_destination("dest_1")]
    return {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": line_item_ids,
          "destinations": destinations,
          "selected_destination_id": selected_destination_id,
        }
      ]
    }

  def _checkout_with_options(self) -> dict:
    """Create a checkout and drive it to a state that carries options.

    Creates a session, then updates it with the configured destination so
    the business generates fulfillment ``options[]``, and returns the raw
    update response JSON.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)
    fulfillment_payload = self._shipping_fulfillment_payload(
      [checkout_obj.line_items[0].id]
    )
    return self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0018",  # option title MUST distinguish siblings
  )
  def test_option_titles_distinguish_siblings(self) -> None:
    """FUL-001: sibling ``options[].title`` are non-empty and distinct.

    Given a checkout whose business has generated fulfillment options,
    When a group carries options,
    Then each option's ``title`` is a non-empty string and no two siblings
    in the same group share a title (fulfillment.md: ``options[].title``
    "MUST distinguish this option from its siblings"). Titles that differ
    only in letter case or spacing don't distinguish anything for a buyer,
    so they count as the same title; that is the reading a machine can
    check, recorded in findings.md.
    """
    response_json = self._checkout_with_options()

    groups_with_options = 0
    for method in _methods(response_json):
      for group in method.get("groups") or []:
        options = group.get("options") or []
        if not options:
          continue
        groups_with_options += 1
        titles = []
        for opt in options:
          title = opt.get("title")
          self.assertIsInstance(
            title,
            str,
            msg=f"Option {opt.get('id')!r} title must be a string: {opt}",
          )
          self.assertTrue(
            title.strip(),
            msg=f"Option {opt.get('id')!r} title must be non-empty: {opt}",
          )
          titles.append(" ".join(title.casefold().split()))
        self.assertEqual(
          len(titles),
          len(set(titles)),
          msg=(
            "Sibling option titles must distinguish the options (distinct "
            "once case and spacing are ignored); got duplicates in "
            f"{[opt.get('title') for opt in options]}"
          ),
        )

    if groups_with_options == 0:
      self.skip_no_trigger(
        "a shipping_locations.domestic_destination the server generates "
        "fulfillment options for"
      )

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0021",  # description MUST NOT repeat title/total
  )
  def test_option_descriptions_do_not_repeat_title_or_total(self) -> None:
    """`options[].description` provides supplementary context only.

    extensions/fulfillment.md (Rendering / Business Responsibilities):
    for `options[].description`, the Business MUST NOT repeat `title` or
    `total`. When the server omits `description` on every option, the test
    skips as not exercised.
    """
    response_json = self._checkout_with_options()

    options_with_description = 0
    for m_idx, method in enumerate(_methods(response_json)):
      for g_idx, group in enumerate(method.get("groups") or []):
        for o_idx, opt in enumerate(group.get("options") or []):
          if not isinstance(opt, dict) or not _option_has_description(opt):
            continue
          options_with_description += 1
          self.assert_option_description(
            opt,
            where=(
              f"fulfillment.methods[{m_idx}].groups[{g_idx}].options[{o_idx}]"
            ),
          )

    if options_with_description == 0:
      self.skip_not_exercised(
        "the server emits no fulfillment options[].description"
      )

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0048",  # single group per method by default
  )
  def test_default_config_single_group_per_method(self) -> None:
    """FUL-008: under default config a method has one consolidating group.

    Given a cart with several line items assigned to one shipping method,
    When the platform declares no ``supports_multi_group`` (the default),
    Then each returned method carries exactly one group, and that group
    covers all of the method's line items (fulfillment.md, Business
    Response Behavior: "Business MUST consolidate all items into a single
    group per method").
    """
    item_id = self.fixture_ctx.get_test_sku()
    price = self.fixture_ctx.get_test_price()
    currency = self.conformance_config.get("currency", "USD")
    version = self.conformance_config.get("ucp_version", "2026-04-08")

    handlers = [
      payment_handler.Base(
        id="google_pay",
        name="google.pay",
        version=version,
        spec="https://example.com/spec",
        config_schema="https://example.com/schema",
        instrument_schemas=["https://example.com/instrument_schema"],
        config={},
      )
    ]

    line_item_ids = ["li_1", "li_2"]
    line_items = []
    for li_id in line_item_ids:
      li = line_item_create_request.LineItemCreateRequest(
        quantity=1,
        item=item_create_request.ItemCreateRequest(id=item_id),
      )
      li.id = li_id
      li.item.price = price
      li.totals = []
      line_items.append(li)

    payment = payment_create_request.PaymentCreateRequest(
      instruments=[],
      handlers=[h.model_dump(mode="json", exclude_none=True) for h in handlers],
    )

    create_req = checkout_create_request.CheckoutCreateRequest(
      id=str(uuid.uuid4()),
      currency=currency,
      line_items=line_items,
      payment=payment,
      fulfillment=self._shipping_fulfillment_payload(line_item_ids),
    )
    create_req.status = "incomplete"
    create_req.ucp = {"version": version}
    create_req.totals = []
    create_req.links = []

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_req.model_dump(mode="json", by_alias=True, exclude_none=True),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, [200, 201])
    response_json = response.json()

    methods = _methods(response_json)
    self.assertTrue(
      methods, msg=f"No fulfillment methods returned: {response_json}"
    )

    methods_with_groups = 0
    for method in methods:
      groups = method.get("groups")
      if not groups:
        # A server MAY defer group generation; only assert when present.
        continue
      methods_with_groups += 1
      self.assertEqual(
        len(groups),
        1,
        msg=(
          "Default config (no supports_multi_group) requires exactly one"
          f" group per method; method {method.get('id')!r} returned"
          f" {len(groups)} groups: {groups}"
        ),
      )
      method_items = set(method.get("line_item_ids") or [])
      group_items = set(groups[0].get("line_item_ids") or [])
      self.assertEqual(
        group_items,
        method_items,
        msg=(
          "The single group must consolidate all of the method's line"
          f" items; method {method.get('id')!r} items {method_items} vs"
          f" group items {group_items}"
        ),
      )

    if methods_with_groups == 0:
      self.skip_no_trigger(
        "a shipping_locations.domestic_destination the server returns "
        "fulfillment groups for"
      )

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0012",  # echoes same selected_destination_id
    "REQ-SHOPPING-FULFILLMENT-0013",  # one matching typed destination entry
  )
  def test_selected_destination_appears_once_and_is_typed(self) -> None:
    """Accepted `selected_destination_id` is echoed with one typed destination.

    extensions/fulfillment.md (Destination Selection): when the Business
    accepts a non-null `selected_destination_id`, its response MUST carry
    that same `selected_destination_id` value on the method and MUST include
    exactly one destination in that same method's `destinations[]` whose
    `id` equals it, typed as specified in Destinations.
    """
    response_json = self._checkout_with_options()
    methods = _methods(response_json)
    self.assertTrue(
      methods, msg=f"No fulfillment methods returned: {response_json}"
    )
    self.assert_selected_destination(
      methods[0],
      expected_id="dest_1",
      expected_type="shipping_address",
      where="fulfillment.methods[0]",
    )

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0016",  # rejected update keeps checkout intact
    "REQ-SHOPPING-FULFILLMENT-0017",  # recoverable error on destination path
  )
  def test_unrecognized_selected_destination_id_rejected(self) -> None:
    """Unrecognized `selected_destination_id` on Update leaves Checkout intact.

    extensions/fulfillment.md (Destination Selection): when the Business
    cannot accept a `selected_destination_id` submitted on Update Checkout
    because the ID is not recognized for that method, it MUST leave the
    current Checkout unchanged and MUST return that Checkout with an error
    Message with `severity: "recoverable"` whose `path` selects the
    attempted method's `selected_destination_id`.
    """
    before_json = self._checkout_with_options()
    before_obj = checkout.Checkout(**before_json)

    rejected_fulfillment = self._shipping_fulfillment_payload(
      [before_obj.line_items[0].id],
      selected_destination_id="unrecognized_dest_id_999",
    )
    after_json = self.update_checkout_session(
      before_obj, fulfillment=rejected_fulfillment
    )
    self.assert_rejected_destination_update(
      before_json, after_json, method_index=0
    )

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0014",  # revalidates selected destination
    "REQ-SHOPPING-FULFILLMENT-0016",  # failed revalidation keeps checkout
    "REQ-SHOPPING-FULFILLMENT-0017",  # recoverable error on destination path
  )
  def test_revalidation_failure_on_selected_destination_rejected(
    self,
  ) -> None:
    """Destination failing revalidation on Update leaves Checkout unchanged.

    extensions/fulfillment.md (Destination Selection): the Business MUST
    revalidate the selected destination's current availability and terms.
    When revalidation fails on Update Checkout, it MUST leave the current
    Checkout unchanged and return a `recoverable` error Message whose `path`
    selects the attempted method's `selected_destination_id`.
    """
    unserviceable_id = self.require_trigger("unserviceable_destination_id")
    before_json = self._checkout_with_options()
    before_obj = checkout.Checkout(**before_json)

    rejected_fulfillment = self._shipping_fulfillment_payload(
      [before_obj.line_items[0].id],
      selected_destination_id=unserviceable_id,
    )
    after_json = self.update_checkout_session(
      before_obj, fulfillment=rejected_fulfillment
    )
    self.assert_rejected_destination_update(
      before_json, after_json, method_index=0
    )

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0002",  # selected_destination_id over context
  )
  def test_selected_destination_id_takes_precedence_over_context_location(
    self,
  ) -> None:
    """`selected_destination_id` takes precedence over `context.location`.

    extensions/fulfillment.md (Location Context): once a Platform sets
    `selected_destination_id` on a Checkout method, a Business MUST use the
    referenced destination rather than `context.location` for that method's
    fulfillment scope.
    """
    baseline_json = self._checkout_with_options()
    baseline_methods = _methods(baseline_json)
    self.assertTrue(
      baseline_methods,
      msg=f"No fulfillment methods in baseline checkout: {baseline_json}",
    )
    baseline_method = baseline_methods[0]
    self.assertEqual(baseline_method.get("selected_destination_id"), "dest_1")

    baseline_obj = checkout.Checkout(**baseline_json)
    with_context_json = self.update_checkout_session(
      baseline_obj,
      fulfillment=self._shipping_fulfillment_payload(
        [baseline_obj.line_items[0].id]
      ),
      context={"location": "loc_conflicting_override"},
    )
    context_methods = _methods(with_context_json)
    self.assertTrue(
      context_methods,
      msg=f"No fulfillment methods with context.location: {with_context_json}",
    )
    context_method = context_methods[0]
    self.assertEqual(
      context_method.get("selected_destination_id"),
      "dest_1",
      msg=(
        "selected_destination_id on method was overridden by context.location "
        "(FULFILLMENT-0002)"
      ),
    )
    self.assertEqual(
      _method_options_signature(context_method),
      _method_options_signature(baseline_method),
      msg=(
        "Method fulfillment options changed when context.location was added "
        "despite selected_destination_id being set on the method "
        "(FULFILLMENT-0002)"
      ),
    )

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0009",  # business_location returns type, id, name
    "REQ-SHOPPING-FULFILLMENT-0012",  # echoes same selected_destination_id
    "REQ-SHOPPING-FULFILLMENT-0013",  # one matching typed destination entry
    "REQ-SHOPPING-FULFILLMENT-0035",  # pickup location ID accepted in checkout
  )
  def test_pickup_location_id_returns_business_location(
    self,
  ) -> None:
    """Pickup `location` ID is accepted as `selected_destination_id`.

    extensions/fulfillment.md (Business Location Destination, Catalog
    Availability): a Business that advertises pickup at a `location` MUST
    accept that same ID as `selected_destination_id` for that method in
    Checkout, and MUST return `type: "business_location"`, `id`, and its
    Buyer-facing `name` in that method's `destinations[]`.
    """
    location_id = self.require_trigger("pickup_location_id")
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    pickup_fulfillment = {
      "methods": [
        {
          "id": "method_pickup_1",
          "type": "pickup",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "selected_destination_id": location_id,
        }
      ]
    }
    updated_json = self.update_checkout_session(
      checkout_obj, fulfillment=pickup_fulfillment
    )
    methods = _methods(updated_json)
    pickup_method = next(
      (m for m in methods if isinstance(m, dict) and m.get("type") == "pickup"),
      None,
    )
    self.assertIsNotNone(
      pickup_method,
      msg=f"No pickup method returned in checkout response: {updated_json}",
    )
    self.assert_selected_destination(
      pickup_method,
      expected_id=location_id,
      expected_type="business_location",
      where="fulfillment.methods[type='pickup']",
    )

  @requires_capability(_CATALOG_LOOKUP_CAPABILITY)
  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0001",  # filters.fulfills_to overrides context
  )
  def test_catalog_fulfills_to_takes_precedence_over_context_location(
    self,
  ) -> None:
    """Catalog `filters.fulfills_to` takes precedence over `context`.

    extensions/fulfillment.md (Location Context): when Catalog
    `filters.fulfills_to` is present, a Business MUST resolve the
    fulfillment destination and method `availability` from it rather than
    from `context`.
    """
    cfg = self.require_trigger("catalog_fulfills_to")
    item_id = cfg["item_id"]
    fulfills_to = cfg["fulfills_to"]
    conflicting_location = cfg["conflicting_location"]

    baseline_resp = self.catalog_lookup(
      [item_id], filters={"fulfills_to": fulfills_to}
    )
    self.assert_response_status(baseline_resp, 200)
    baseline_methods = _variant_fulfillment_methods(
      baseline_resp.json(), item_id
    )
    self.assertTrue(
      baseline_methods,
      msg=(
        f"No variant fulfillment.methods returned for {item_id!r} with "
        f"filters.fulfills_to={fulfills_to}: {baseline_resp.json()}"
      ),
    )

    combined_resp = self.catalog_lookup(
      [item_id],
      filters={"fulfills_to": fulfills_to},
      context={"location": conflicting_location},
    )
    self.assert_response_status(combined_resp, 200)
    combined_methods = _variant_fulfillment_methods(
      combined_resp.json(), item_id
    )

    def _scope_summary(
      methods: list[dict[str, Any]],
    ) -> list[tuple[Any, Any, Any]]:
      return [
        (m.get("type"), m.get("location"), m.get("availability"))
        for m in methods
        if isinstance(m, dict)
      ]

    self.assertEqual(
      _scope_summary(combined_methods),
      _scope_summary(baseline_methods),
      msg=(
        "Catalog variant fulfillment destination or availability differed "
        "when context.location was passed alongside filters.fulfills_to "
        "(FULFILLMENT-0001)"
      ),
    )


if __name__ == "__main__":
  absltest.main()

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

"""A scripted checkout server with the postures the sample server lacks.

The sample server completes a checkout without a buyer, answers business
outcomes with a 4xx and always returns a ``payment`` member, so it cannot
show whether the suite copes with a business that does otherwise, within
what the spec allows. This store does otherwise:

* it needs a buyer email to place an order, and answers a completion
  without one the way the spec's own Create Checkout example answers a
  create without one: HTTP 200, ``status: incomplete`` and a recoverable
  error at ``$.buyer.email``;
* it returns no ``payment`` member (the schema leaves it optional);
* it refuses calls on a terminal session in-band: HTTP 200, an Error
  Response with ``ucp.status: "error"`` and no resource;
* it builds the fulfillment options itself from the destination the
  platform sends, and reports an unknown ``selected_option_id`` as an error;
* it serves Get Order and, like the spec, defines no way to edit an order:
  ``PUT /orders/{id}`` is answered 405;
* it offers a buyer it knows their saved addresses as destinations only on
  a request that carries that buyer's bearer token, as identity linking
  requires of a business from 2026-08-25 (the sample server goes by the
  buyer's email alone, which ``stored_state="email"`` reproduces);
* it comes to the situations the sample server ties to an item by other
  means, when told to: a completion stays in progress because of the
  payment token used, and the tax is itemized because of where the order
  ships;
* it updates an order through an admin route of its own,
  ``POST /admin/orders/{id}/update`` with an ``X-Admin-Secret`` header, and
  keeps the order events it would deliver in ``StrictStore.deliveries``;
* it takes discount codes when given some, allocates each discount to the
  line items and so reports it as an ``items_discount`` total, as
  discount.md has it, where the sample server reports ``discount``;
* it returns a ``continue_url`` when given the address to build it from,
  which the sample server leaves out of a checkout that isn't escalated,
  and hands a checkout for an item it is told about to the buyer
  (``requires_escalation``).

It serves ``httpx`` through a ``MockTransport``, so a test drives the suite's
own helpers against it without a socket. ``StrictStore.requests`` keeps
every request, for assertions on what the suite sent.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
import json
from typing import Any
import unittest

from framework.outcomes import skip_category
import httpx
from shopping import base

ORIGIN = "https://store.example"
ENDPOINT = f"{ORIGIN}/ucp"
# The request that updates an order, as a fixture file would declare it.
ORDER_UPDATE_REQUEST = {
  "url": "/admin/orders/{order_id}/update",
  "headers": {"X-Admin-Secret": "{simulation_secret}"},
}

_OPTIONS = [
  {
    "id": "ground",
    "title": "Ground",
    "totals": [{"type": "total", "amount": 599}],
  },
  {
    "id": "express",
    "title": "Express",
    "totals": [{"type": "total", "amount": 1599}],
  },
]


def _error(code: str, path: str, content: str) -> dict[str, Any]:
  return {
    "type": "error",
    "code": code,
    "path": path,
    "content": content,
    "severity": "recoverable",
  }


def wired(
  case: type[base.ShoppingIntegrationTestBase],
  method: str,
  store: StrictStore,
  fixtures: dict[str, Any] | None = None,
) -> base.ShoppingIntegrationTestBase:
  """Return a suite test whose requests land in `store`.

  The test's `setUp` is not run: it would look for a server. The caller
  calls the test method itself.

  Args:
    case: The suite's test class.
    method: The name of the test method the instance is for.
    store: The scripted store that answers its requests.
    fixtures: What `test_fixtures` declares, such as a `known_customer`.

  """
  test = case(method)
  test.client = store.client()
  test._shopping_service_endpoint = ENDPOINT
  test.conformance_config = {}
  test.fixture_ctx = base.DynamicFixtureContext(
    {"test_fixtures": fixtures or {}}
  )
  # The polls for a completion and for an order update wait for real time
  # otherwise.
  test.COMPLETION_POLL_SECONDS = 0
  test.ORDER_UPDATE_POLL_SECONDS = 0
  return test


def skipped(
  function: Callable[..., Any], *arguments: Any, **options: Any
) -> tuple[str, str]:
  """Call what must skip the test; return the skip's category and detail."""
  try:
    function(*arguments, **options)
  except unittest.SkipTest as skip:
    return skip_category(str(skip))
  raise AssertionError("the call did not skip the test")


def failure(
  function: Callable[..., Any], *arguments: Any, **options: Any
) -> str:
  """Call what must fail the test; return the failure's message.

  A skip is not a failure. `assertRaises(AssertionError)` lets a
  `SkipTest` through, which then skips the unit test instead of failing it;
  here it is an error of its own.
  """
  try:
    function(*arguments, **options)
  except unittest.SkipTest as skip:
    raise RuntimeError(f"the call skipped the test: {skip}") from skip
  except AssertionError as failed:
    return str(failed)
  raise AssertionError("the call did not fail the test")


class StrictStore:
  """The scripted store; see the module docstring for its postures."""

  def __init__(
    self,
    *,
    needs_buyer: bool = True,
    tax: int = 0,
    async_completion: bool = False,
    order_get: int = 200,
    order_put: int = 405,
    customers: dict[str, dict[str, Any]] | None = None,
    stored_state: str = "authenticated",
    async_tokens: frozenset[str] | set[str] = frozenset(),
    tax_lines_by_item: dict[str, list[tuple[str, int]]] | None = None,
    tax_lines_by_country: dict[str, list[tuple[str, int]]] | None = None,
    tax_lines_off_by: int = 0,
    order_update: str | None = None,
    update_secret: str | None = "admin-secret",
    update_refusal: int = 403,
    update_delivery: str = "full",
    discount_codes: dict[str, int] | None = None,
    discount_total: str = "items_discount",
    automatic_discount: int = 0,
    discount_fault: str | None = None,
    continue_url: str | None = None,
    escalation_items: frozenset[str] | set[str] = frozenset(),
  ) -> None:
    """Configure the store.

    Args:
      needs_buyer: Refuse to place an order until the checkout has a buyer
        email, with a recoverable error at `$.buyer.email`.
      tax: A tax amount (minor units) added to the totals when non-zero.
      async_completion: Answer Complete Checkout with
        `complete_in_progress` and place the order on the next Get.
      order_get: The status Get Order answers with. Anything but 200 is a
        refusal without the order, such as a 401.
      order_put: The status `PUT /orders/{id}` answers with. 405 unless
        told otherwise, since the spec defines only Get Order; with 200 the
        store keeps the body and returns it, as the sample server's own
        endpoint does.
      customers: The buyers the store knows, by email, each as
        `{"token": ..., "addresses": [...]}`: the bearer token that
        authenticates the buyer and the addresses the store keeps for them.
      stored_state: When the store answers from what it keeps for a buyer.
        "authenticated": only on a request with `Authorization: Bearer`
        and the buyer's token. "email": on any request that names the
        buyer's email, as the sample server does.
      async_tokens: Payment tokens the store takes time over: a completion
        that pays with one is answered `complete_in_progress`, and an
        update that arrives meanwhile is refused with a recoverable error.
      tax_lines_by_item: For an item id, the tax its checkout carries, as
        `(display_text, amount)` sub-lines of one `tax` total.
      tax_lines_by_country: The same for the `address_country` of the
        selected destination.
      tax_lines_off_by: Added to the itemized tax total's `amount`, for a
        store whose sub-lines don't add up to it.
      order_update: What `POST /admin/orders/{id}/update` does to an order.
        None: the store has no such route (404). "ship": a `shipped`
        fulfillment event is added. "refund": an adjustment is added, and
        nothing ships. "nothing": the request is accepted and the order
        stays as it was.
      update_secret: The `X-Admin-Secret` the route takes. None for a
        store that takes any caller.
      update_refusal: The status it answers without that secret: 403, 401,
        or 404 for a store that reveals nothing to other callers.
      update_delivery: The order event the store delivers after an update.
        "full": the order as it now is. "stale": the order as it was.
        "delta": the order's id and the member that changed. "none": no
        delivery at all.
      discount_codes: The codes the store takes, each with the amount it
        takes off, in minor units; they match in any case. Without any the
        store keeps the submitted codes and prices no discount.
      discount_total: The `totals[]` type a code's discount is reported
        under. "items_discount": the discount is allocated to the line
        items. "discount": it has no allocations.
      automatic_discount: An amount the store takes off every checkout on
        its own account, with no code, reported as `items_discount`.
      discount_fault: A way the store gets discounts wrong. "sticky": a
        code's discount stays when the codes are cleared. "any_code": a
        code it doesn't know is taken for one it does. "positive": a
        code's discount total carries a positive amount. "case_sensitive":
        a code in another case isn't matched. "unpriced": a code is listed
        as applied and takes nothing off the totals.
      continue_url: What the store builds a checkout's `continue_url`
        from: the checkout's id is added to it, and the URL is returned on
        every checkout that isn't completed or canceled. None for a store
        that returns no `continue_url`.
      escalation_items: Items the store won't sell without the buyer: a
        checkout for one, with nothing else missing, is
        `requires_escalation`, with a message for the buyer.

    """
    self.needs_buyer = needs_buyer
    self.tax = tax
    self.async_completion = async_completion
    self.order_get = order_get
    self.order_put = order_put
    self.customers = customers or {}
    self.stored_state = stored_state
    self.async_tokens = frozenset(async_tokens)
    self.tax_lines_by_item = tax_lines_by_item or {}
    self.tax_lines_by_country = tax_lines_by_country or {}
    self.tax_lines_off_by = tax_lines_off_by
    self.order_update = order_update
    self.update_secret = update_secret
    self.update_refusal = update_refusal
    self.update_delivery = update_delivery
    self.discount_codes = discount_codes
    self.discount_total = discount_total
    self.automatic_discount = automatic_discount
    self.discount_fault = discount_fault
    self.continue_url = continue_url
    self.escalation_items = frozenset(escalation_items)
    self.checkouts: dict[str, dict[str, Any]] = {}
    self.orders: dict[str, dict[str, Any]] = {}
    self.requests: list[httpx.Request] = []
    # The order events the store delivers, as a webhook receiver holds them.
    self.deliveries: list[dict[str, Any]] = []
    self._pending: set[str] = set()
    self._counter = 0
    # The Authorization header of the request being answered.
    self._authorization: str | None = None

  # -- httpx plumbing -------------------------------------------------------

  def client(self) -> httpx.Client:
    """Return a client whose every request lands in this store.

    A path without a host is on the store's own address, as a path is on
    --server_url in a run.
    """
    return httpx.Client(
      base_url=ORIGIN, transport=httpx.MockTransport(self.handle)
    )

  def handle(self, request: httpx.Request) -> httpx.Response:
    """Route one request."""
    self.requests.append(request)
    self._authorization = request.headers.get("authorization")
    path = request.url.path
    body = json.loads(request.content) if request.content else {}
    admin = path.strip("/").split("/")
    if len(admin) == 4 and admin[:2] == ["admin", "orders"]:
      if admin[3] != "update" or self.order_update is None:
        return httpx.Response(404, json={"detail": f"no route {path}"})
      if request.method != "POST":
        return httpx.Response(405, json={"detail": "Method Not Allowed"})
      return self._update_order(admin[2], request.headers)
    prefix = httpx.URL(ENDPOINT).path
    assert path.startswith(prefix), path
    route = path[len(prefix) :].strip("/").split("/")

    if route == ["checkout-sessions"] and request.method == "POST":
      return self._create(body)
    if len(route) == 2 and route[0] == "checkout-sessions":
      checkout = self.checkouts.get(route[1])
      if checkout is None:
        return self._not_found(route[1])
      if request.method == "GET":
        return self._get(checkout)
      if request.method == "PUT":
        return self._update(checkout, body)
    if len(route) == 3 and route[0] == "checkout-sessions":
      checkout = self.checkouts.get(route[1])
      if checkout is None:
        return self._not_found(route[1])
      if route[2] == "complete" and request.method == "POST":
        return self._complete(checkout, body)
      if route[2] == "cancel" and request.method == "POST":
        return self._cancel(checkout)
    if len(route) == 2 and route[0] == "orders":
      return self._order(request.method, route[1], body)
    return httpx.Response(404, json={"detail": f"no route {path}"})

    # -- what the store does ------------------------------------------------

  def _new_id(self, prefix: str) -> str:
    self._counter += 1
    return f"{prefix}_{self._counter}"

  def _order(
    self, method: str, order_id: str, body: dict[str, Any]
  ) -> httpx.Response:
    """Answer a request on an order: Get Order, and PUT as configured."""
    if method == "GET":
      if self.order_get != 200:
        return httpx.Response(self.order_get, json={"detail": "Refused"})
      if order_id not in self.orders:
        return httpx.Response(404, json={"detail": "Order not found"})
      return httpx.Response(200, json=copy.deepcopy(self.orders[order_id]))
    if method != "PUT" or self.order_put == 405:
      return httpx.Response(405, json={"detail": "Method Not Allowed"})
    if self.order_put != 200:
      return httpx.Response(self.order_put, json={"detail": "Refused"})
    if order_id not in self.orders:
      return httpx.Response(404, json={"detail": "Order not found"})
    self.orders[order_id] = copy.deepcopy(body)
    return httpx.Response(200, json=body)

  def _update_order(
    self, order_id: str, headers: httpx.Headers
  ) -> httpx.Response:
    """Answer the admin route: update the order, then deliver the event."""
    if (
      self.update_secret is not None
      and headers.get("x-admin-secret") != self.update_secret
    ):
      return httpx.Response(self.update_refusal, json={"detail": "Refused"})
    order = self.orders.get(order_id)
    if order is None:
      return httpx.Response(404, json={"detail": "Order not found"})
    before = copy.deepcopy(order)
    changed = None
    if self.order_update == "ship":
      changed = "fulfillment"
      order["fulfillment"]["events"].append(
        {
          "id": self._new_id("evt"),
          "occurred_at": "2026-11-18T10:00:00Z",
          "type": "shipped",
          "line_items": [
            {"id": li["id"], "quantity": li["quantity"]["total"]}
            for li in order["line_items"]
          ],
        }
      )
      for li in order["line_items"]:
        li["quantity"]["fulfilled"] = li["quantity"]["total"]
        li["status"] = "fulfilled"
    elif self.order_update == "refund":
      changed = "adjustments"
      order["adjustments"] = [
        {
          "id": self._new_id("adj"),
          "type": "refund",
          "occurred_at": "2026-11-18T10:00:00Z",
          "status": "completed",
          "totals": [{"type": "total", "amount": -100}],
        }
      ]
    if self.update_delivery == "full":
      self._deliver(order)
    elif self.update_delivery == "stale":
      self._deliver(before)
    elif self.update_delivery == "delta" and changed:
      self._deliver({"id": order_id, changed: order[changed]})
    return httpx.Response(200, json={"status": "accepted"})

  def _deliver(self, order: dict[str, Any]) -> None:
    """Keep an order event the way the suite's webhook receiver does."""
    self.deliveries.append(
      {"payload": copy.deepcopy(order), "headers": {}, "response_status": 200}
    )

  def _create(self, body: dict[str, Any]) -> httpx.Response:
    checkout: dict[str, Any] = {
      "ucp": {"version": "2026-08-25"},
      "id": self._new_id("chk"),
      "status": "incomplete",
      "currency": "USD",
      "line_items": [
        {
          "id": self._new_id("li"),
          "item": {"id": li["item"]["id"], "title": "Thing", "price": 105000},
          "quantity": li["quantity"],
          "totals": [
            {"type": "subtotal", "amount": 105000 * li["quantity"]},
            {"type": "total", "amount": 105000 * li["quantity"]},
          ],
        }
        for li in body.get("line_items", [])
      ],
      "links": [{"type": "privacy_policy", "url": "https://store.example/p"}],
      "messages": [],
    }
    if body.get("buyer"):
      checkout["buyer"] = dict(body["buyer"])
    self._apply_fulfillment(checkout, body.get("fulfillment"))
    self._settle(checkout)
    self.checkouts[checkout["id"]] = checkout
    return httpx.Response(201, json=self._view(checkout))

  def _get(self, checkout: dict[str, Any]) -> httpx.Response:
    if checkout["id"] in self._pending:
      self._pending.discard(checkout["id"])
      self._place(checkout)
    return httpx.Response(200, json=self._view(checkout))

  def _update(
    self, checkout: dict[str, Any], body: dict[str, Any]
  ) -> httpx.Response:
    if checkout["status"] in ("completed", "canceled"):
      return self._error_response(
        "invalid_parameter",
        "$.status",
        f"Cannot update {checkout['status']} checkout: {checkout['id']}",
      )
    if checkout["status"] == "complete_in_progress":
      # An accepted completion is under way: the checkout as it is, with a
      # recoverable error (checkout/index.md, Accepted completion).
      view = self._view(checkout)
      view["messages"] = [
        _error(
          "checkout_in_progress",
          "$.status",
          "The checkout is being completed and cannot be updated now.",
        )
      ]
      return httpx.Response(200, json=view)
    if "buyer" in body:
      checkout["buyer"] = dict(body["buyer"]) if body["buyer"] else None
    if body.get("line_items"):
      for li in checkout["line_items"]:
        for wanted in body["line_items"]:
          if wanted.get("id") == li["id"]:
            li["quantity"] = wanted.get("quantity", li["quantity"])
            li["totals"] = [
              {"type": "subtotal", "amount": 105000 * li["quantity"]},
              {"type": "total", "amount": 105000 * li["quantity"]},
            ]
    if "fulfillment" in body:
      self._apply_fulfillment(checkout, body["fulfillment"])
    if body.get("discounts"):
      codes = body["discounts"].get("codes", [])
      if codes or self.discount_fault != "sticky":
        checkout["discounts"] = {"codes": codes}
    self._settle(checkout)
    return httpx.Response(200, json=self._view(checkout))

  def _complete(
    self, checkout: dict[str, Any], body: dict[str, Any]
  ) -> httpx.Response:
    if checkout["status"] in ("completed", "canceled"):
      return self._error_response(
        "invalid_parameter",
        "$.status",
        f"Cannot complete already {checkout['status']} checkout:"
        f" {checkout['id']}",
      )
    self._settle(checkout)
    if checkout["status"] != "ready_for_complete":
      # The spec's posture for a blocked completion: the current checkout,
      # still incomplete, with the recoverable errors that block it.
      return httpx.Response(200, json=self._view(checkout))
    token = (
      ((body.get("payment") or {}).get("instruments") or [{}])[0]
      .get("credential", {})
      .get("token")
    )
    if token == "fail_token":
      checkout["status"] = "incomplete"
      checkout["messages"] = [
        _error("payment_declined", "$.payment", "The card was declined.")
      ]
      return httpx.Response(200, json=self._view(checkout))
    if self.async_completion or token in self.async_tokens:
      checkout["status"] = "complete_in_progress"
      self._pending.add(checkout["id"])
      return httpx.Response(200, json=self._view(checkout))
    self._place(checkout)
    return httpx.Response(200, json=self._view(checkout))

  def _cancel(self, checkout: dict[str, Any]) -> httpx.Response:
    if checkout["status"] == "completed":
      return self._error_response(
        "invalid_parameter",
        "$.status",
        f"Cannot cancel completed checkout: {checkout['id']}",
      )
    if checkout["status"] == "canceled":
      return self._not_found(checkout["id"])
    checkout["status"] = "canceled"
    return httpx.Response(200, json=self._view(checkout))

  def _place(self, checkout: dict[str, Any]) -> None:
    checkout["status"] = "completed"
    checkout["messages"] = []
    order_id = self._new_id("ord")
    permalink_url = f"https://store.example/orders/{checkout['id']}"
    checkout["order"] = {"id": order_id, "permalink_url": permalink_url}
    self.orders[order_id] = {
      "ucp": {"version": "2026-08-25"},
      "id": order_id,
      "checkout_id": checkout["id"],
      "permalink_url": permalink_url,
      "line_items": [
        {
          "id": li["id"],
          "item": dict(li["item"]),
          "quantity": {"total": li["quantity"], "fulfilled": 0},
          "totals": copy.deepcopy(li["totals"]),
          "status": "processing",
        }
        for li in checkout["line_items"]
      ],
      "fulfillment": {"expectations": [], "events": []},
      "currency": checkout["currency"],
      "totals": copy.deepcopy(checkout["totals"]),
    }
    self._deliver(self.orders[order_id])

  def _apply_fulfillment(
    self, checkout: dict[str, Any], fulfillment: dict[str, Any] | None
  ) -> None:
    """Build the method, its groups and options from what the platform sent."""
    if not fulfillment or not fulfillment.get("methods"):
      checkout.pop("fulfillment", None)
      return
    sent = fulfillment["methods"][0]
    destinations = self._with_stored_addresses(
      checkout,
      [
        {**d, "id": d.get("id") or self._new_id("dest")}
        for d in sent.get("destinations", [])
      ],
    )
    selected_destination = sent.get("selected_destination_id")
    if selected_destination not in {d["id"] for d in destinations}:
      selected_destination = None
    sent_groups = sent.get("groups") or [{}]
    selected_option = sent_groups[0].get("selected_option_id")
    problems = []
    if selected_option and selected_option not in {o["id"] for o in _OPTIONS}:
      problems.append(
        _error(
          "option_unavailable",
          "$.fulfillment.methods[0].groups[0].selected_option_id",
          f"The shipping option {selected_option!r} is not available.",
        )
      )
      selected_option = None
    for d in destinations:
      if not d.get("address_locality"):
        problems.append(
          _error(
            "missing",
            "$.fulfillment.methods[0].destinations[0].address_locality",
            "The city is required.",
          )
        )
    checkout["fulfillment"] = {
      "methods": [
        {
          "id": "method_1",
          "type": "shipping",
          "line_item_ids": [li["id"] for li in checkout["line_items"]],
          "destinations": destinations,
          "selected_destination_id": selected_destination,
          "groups": [
            {
              "id": "group_1",
              "line_item_ids": [li["id"] for li in checkout["line_items"]],
              "options": copy.deepcopy(_OPTIONS),
              "selected_option_id": selected_option,
            }
          ],
        }
      ]
    }
    checkout["_fulfillment_problems"] = problems

  def _stored_addresses(self, checkout: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the addresses kept for the checkout's buyer, when it may.

    The buyer is known by the email on the checkout. In the "authenticated"
    posture the request must carry the buyer's bearer token as well: stored
    user state goes to a user-authenticated request only.
    """
    email = (checkout.get("buyer") or {}).get("email")
    customer = self.customers.get(email)
    if customer is None:
      return []
    if (
      self.stored_state == "authenticated"
      and self._authorization != f"Bearer {customer['token']}"
    ):
      return []
    return customer.get("addresses", [])

  def _with_stored_addresses(
    self, checkout: dict[str, Any], destinations: list[dict[str, Any]]
  ) -> list[dict[str, Any]]:
    """Add the buyer's stored addresses to the destinations the platform sent.

    Each stored address has a stable id of the store's own. A destination
    the platform sent with the content of a stored address comes back under
    that id, once.
    """
    stored = {
      f"addr_{index}": address
      for index, address in enumerate(self._stored_addresses(checkout), 1)
    }
    merged = []
    for destination in destinations:
      for stored_id, address in stored.items():
        if all(destination.get(k) == v for k, v in address.items()):
          destination = {**destination, "id": stored_id}
      merged.append(destination)
    present = {d["id"] for d in merged}
    merged.extend(
      {"id": stored_id, "type": "shipping_address", **address}
      for stored_id, address in stored.items()
      if stored_id not in present
    )
    return merged

  def _settle(self, checkout: dict[str, Any]) -> None:
    """Recompute messages, totals and status from the checkout's state."""
    if checkout["status"] in ("completed", "canceled", "complete_in_progress"):
      return
    problems: list[dict[str, Any]] = list(
      checkout.get("_fulfillment_problems") or []
    )
    method = (checkout.get("fulfillment") or {}).get("methods", [None])[0]
    option = None
    if method is None or not method.get("selected_destination_id"):
      problems.append(
        _error(
          "address_unverifiable",
          "$.fulfillment",
          "Shipping address is required to complete checkout.",
        )
      )
    else:
      selected = method["groups"][0].get("selected_option_id")
      option = next((o for o in _OPTIONS if o["id"] == selected), None)
      if option is None:
        problems.append(
          _error(
            "missing",
            "$.fulfillment.methods[0].groups[0].selected_option_id",
            "Choose a shipping option.",
          )
        )
    if self.needs_buyer and not (checkout.get("buyer") or {}).get("email"):
      problems.append(
        _error(
          "missing_buyer_info",
          "$.buyer.email",
          "Buyer email is required to complete checkout.",
        )
      )
    subtotal = sum(li["totals"][0]["amount"] for li in checkout["line_items"])
    shipping = option["totals"][0]["amount"] if option else 0
    totals = [{"type": "subtotal", "amount": subtotal}]
    discounts = self._discounts(checkout)
    priced = [
      (of, entry)
      for of, entry in discounts
      if "code" not in entry or self.discount_fault != "unpriced"
    ]
    for kind in ("items_discount", "discount"):
      # The amount is negative; the faulty store gets a code's wrong.
      amount = sum(
        entry["amount"]
        if "code" in entry and self.discount_fault == "positive"
        else -entry["amount"]
        for of, entry in priced
        if of == kind
      )
      if amount:
        totals.append({"type": kind, "amount": amount})
    discounted = sum(entry["amount"] for _, entry in priced)
    if self.discount_codes is not None or self.automatic_discount:
      checkout["discounts"] = {
        "codes": list((checkout.get("discounts") or {}).get("codes", [])),
        "applied": [entry for _, entry in discounts],
      }
    totals.append({"type": "fulfillment", "amount": shipping})
    tax = self.tax or 0
    tax_lines = self._tax_lines(checkout)
    if tax_lines:
      tax = sum(amount for _, amount in tax_lines) + self.tax_lines_off_by
      totals.append(
        {
          "type": "tax",
          "amount": tax,
          "lines": [
            {"display_text": text, "amount": amount}
            for text, amount in tax_lines
          ],
        }
      )
    elif tax:
      totals.append({"type": "tax", "amount": tax})
    totals.append(
      {"type": "total", "amount": subtotal - discounted + shipping + tax}
    )
    checkout["totals"] = totals
    checkout["messages"] = problems
    checkout["status"] = "incomplete" if problems else "ready_for_complete"
    if not problems and any(
      li["item"]["id"] in self.escalation_items for li in checkout["line_items"]
    ):
      # Nothing the platform can supply is missing; the rest is the buyer's.
      checkout["status"] = "requires_escalation"
      checkout["messages"] = [
        {
          "type": "error",
          "code": "buyer_confirmation_required",
          "content": "The buyer has to confirm this purchase with the store.",
          "severity": "requires_buyer_input",
        }
      ]

  def _discounts(
    self, checkout: dict[str, Any]
  ) -> list[tuple[str, dict[str, Any]]]:
    """Return a checkout's discounts: the totals type and applied entry of each.

    The store's own discount comes first, then one for each submitted code
    it takes. A discount reported as `items_discount` carries the
    allocation to the line that makes it one.
    """
    line = "$.line_items[0]"
    found: list[tuple[str, dict[str, Any]]] = []
    if self.automatic_discount:
      found.append(
        (
          "items_discount",
          {
            "title": "Sale",
            "amount": self.automatic_discount,
            "automatic": True,
            "allocations": [{"path": line, "amount": self.automatic_discount}],
          },
        )
      )
    known = self.discount_codes or {}
    if self.discount_fault != "case_sensitive":
      known = {code.upper(): amount for code, amount in known.items()}
    for code in (checkout.get("discounts") or {}).get("codes", []):
      amount = known.get(
        code if self.discount_fault == "case_sensitive" else code.upper()
      )
      if amount is None and self.discount_fault == "any_code" and known:
        amount = next(iter(known.values()))
      if amount is None:
        continue
      entry: dict[str, Any] = {
        "code": code,
        "title": f"Code {code}",
        "amount": amount,
      }
      if self.discount_total == "items_discount":
        entry["allocations"] = [{"path": line, "amount": amount}]
      found.append((self.discount_total, entry))
    return found

  def _tax_lines(self, checkout: dict[str, Any]) -> list[tuple[str, int]]:
    """Return the tax sub-lines of a checkout: by item, else by destination."""
    for li in checkout["line_items"]:
      if li["item"]["id"] in self.tax_lines_by_item:
        return self.tax_lines_by_item[li["item"]["id"]]
    method = (checkout.get("fulfillment") or {}).get("methods", [None])[0]
    if method is None:
      return []
    selected = next(
      (
        d
        for d in method["destinations"]
        if d["id"] == method.get("selected_destination_id")
      ),
      {},
    )
    return self.tax_lines_by_country.get(selected.get("address_country"), [])

  # -- response shapes ------------------------------------------------------

  def _view(self, checkout: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in checkout.items() if not k.startswith("_")}
    if view.get("buyer") is None:
      view.pop("buyer", None)
    if self.continue_url and view["status"] not in ("completed", "canceled"):
      view["continue_url"] = f"{self.continue_url}/{view['id']}"
    return view

  @staticmethod
  def _error_response(code: str, path: str, content: str) -> httpx.Response:
    return httpx.Response(
      200,
      json={
        "ucp": {"version": "2026-08-25", "status": "error"},
        "messages": [_error(code, path, content)],
      },
    )

  @staticmethod
  def _not_found(checkout_id: str) -> httpx.Response:
    return httpx.Response(
      200,
      json={
        "ucp": {"version": "2026-08-25", "status": "error"},
        "messages": [
          {
            "type": "error",
            "code": "not_found",
            "content": f"Checkout '{checkout_id}' not found.",
            "severity": "unrecoverable",
          }
        ],
      },
    )

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

"""Structural conformance for the order-event webhook contract (order.md).

The 2026-04-08 order specification pins a delivery contract for order-event
webhooks beyond "an event arrives" (which webhook_test.py already covers):

  * Required headers follow Standard Webhooks: ``Webhook-Id`` (unique event
    identifier) and ``Webhook-Timestamp`` (unix event timestamp) — order.md
    "Order Event Webhook", Required Headers.
  * The body is the FULL order entity: the same current-state snapshot as Get
    Order. Businesses MUST send the "Order created" event with a fully
    populated order entity and MUST send the full entity on updates, never an
    incremental delta — order.md "Events" and its Business guidelines. Each
    delivery is therefore compared, member for member, with Get Order fetched
    right after it, and validated against the order schema.
  * Deliveries MUST be signed. What is checked here is the signing ENVELOPE
    (headers, digest, signed components, the RFC 9421 shape of
    ``Signature-Input`` and ``Signature``), not cryptographic verification of
    the signature value against the business's published keys: ``UCP-Agent``
    (business profile URL), ``Signature``, ``Signature-Input`` and
    ``Content-Digest`` headers, with
    the SHA-256 ``Content-Digest`` matching the raw body — order.md "Webhook
    Signature Verification". Because the ``UCP-Agent`` header is required on
    webhook deliveries, ``ucp-agent`` is a required signed component
    (signatures.md, REST Request Signing component table, footnote **).

A server that sends unsigned deliveries fails the envelope test (order.md:
payloads MUST be signed); the checks that read a signature's parts then skip
as not exercised. The update-event test skips, naming the trigger, when the
server exposes no /testing/simulate-shipping path.
"""

import base64
import binascii
import hashlib
import re
import time
from typing import Any

from absl.testing import absltest
from framework.base_test import FLAGS
from framework.decorators import (
  no_rule,
  older_version,
  partly,
  requires_capability,
  spec_assert,
)
from framework.mock_webhook_server import MockWebhookServer
from pydantic import ValidationError
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import order

_ORDER_CAPABILITY = "dev.ucp.shopping.order"

# order.json "required" — the floor for a "fully populated order entity"
_ORDER_REQUIRED = (
  "ucp",
  "id",
  "checkout_id",
  "permalink_url",
  "line_items",
  "fulfillment",
  "currency",
  "totals",
)

# order.md "Webhook Signature Verification" Required Headers (lowercase)
_SIGNATURE_HEADERS = (
  "ucp-agent",
  "signature",
  "signature-input",
  "content-digest",
)

# RFC 9421 §4.1-4.2, on RFC 8941 structured fields: Signature-Input and
# Signature are Dictionaries sharing their labels. A Signature-Input member
# is an Inner List of quoted component names followed by parameters
# (``sig1=("@method" "content-digest");keyid="merchant-2026"``); the
# Signature member with the same label is a Byte Sequence (``sig1=:base64:``).
_SF_KEY = r"[a-z*][a-z0-9_.*-]*"
_SF_PARAMS = rf'(?:;{_SF_KEY}(?:=(?:"[^"]*"|[^;,\s]+))?)*'
_SIGNATURE_INPUT_MEMBER = re.compile(
  rf"(?P<label>{_SF_KEY})=\((?P<components>[^)]*)\)(?P<params>{_SF_PARAMS})"
)
_SIGNATURE_MEMBER = re.compile(
  rf"(?P<label>{_SF_KEY})=:(?P<value>[A-Za-z0-9+/=]*):"
)
_COMPONENT_ITEM = rf'"[^"]+"{_SF_PARAMS}'
_COMPONENT_LIST = re.compile(rf"{_COMPONENT_ITEM}(?:\s+{_COMPONENT_ITEM})*")


@requires_capability(_ORDER_CAPABILITY)
class WebhookStructureTest(ShoppingIntegrationTestBase):
  """Order-event webhook contract: headers, full entity, signing."""

  def setUp(self) -> None:
    """Start the webhook receiver."""
    super().setUp()
    self.skip_unless_webhooks_reachable()
    port = FLAGS.mock_webhook_port
    self.webhook_server = MockWebhookServer(port=port)
    self.webhook_server.start()

  def tearDown(self) -> None:
    """Stop the receiver."""
    if hasattr(self, "webhook_server"):
      self.webhook_server.stop()
    super().tearDown()

  # ── shared drivers ──────────────────────────────────────────────────────
  def _events_for(self, order_id: str) -> list[dict[str, Any]]:
    """All captured deliveries whose payload is the given order.

    A delivery whose body has no ``id`` at all (a delta rather than the full
    entity) cannot be matched here; callers surface that case explicitly so
    the deviation reads "not a full entity", not "nothing delivered".
    """
    return [
      e
      for e in self.webhook_server.events
      if isinstance(e.get("payload"), dict)
      and e["payload"].get("id") == order_id
    ]

  def _idless_deliveries(self) -> int:
    """Deliveries whose dict body carries no id (delta-shaped)."""
    return sum(
      1
      for e in self.webhook_server.events
      if isinstance(e.get("payload"), dict) and "id" not in e["payload"]
    )

  def _wait_for_events(self, order_id: str, count: int) -> list[dict[str, Any]]:
    """Poll (up to 5s) until `count` deliveries for the order arrived."""
    for _ in range(50):
      events = self._events_for(order_id)
      if len(events) >= count:
        return events
      time.sleep(0.1)
    return self._events_for(order_id)

  def _place_order(self) -> tuple[str, str, dict[str, Any]]:
    """Create + complete a checkout; return (checkout_id, order_id, event).

    The returned event is the first delivery for the order — the "Order
    created" event the business MUST send (order.md Business guidelines).
    """
    checkout_data = self.create_checkout_session(headers=self.get_headers())
    checkout_id = checkout_data["id"]
    complete_response = self.complete_checkout_session(checkout_id)
    order_id = complete_response["order"]["id"]
    events = self._wait_for_events(order_id, 1)
    if not events and self._idless_deliveries():
      self.fail(
        "webhook delivery arrived without an order id in the body - a delta "
        "rather than the full order entity (order.md Events: deliveries are "
        "fully populated order objects, never deltas)"
      )
    self.assertTrue(
      events,
      "no order-event webhook delivered for the completed order "
      "(order.md: MUST send 'Order created' event)",
    )
    return checkout_id, order_id, events[0]

  def _assert_full_order_entity(
    self, payload: dict[str, Any], checkout_id: str, order_id: str, what: str
  ) -> None:
    """Assert a delivery's body is the fully populated order entity.

    Three checks: the body validates against the order schema; it names our
    order and the checkout it came from; and it is the same current-state
    snapshot Get Order returns, member for member (order.md: the same
    schema for retrieval and delivery, the full entity on every response).

    Args:
      payload: The delivery's body.
      checkout_id: The checkout the order came from.
      order_id: The order's ID.
      what: Which delivery, for the messages.

    """
    missing = [k for k in _ORDER_REQUIRED if payload.get(k) is None]
    self.assertFalse(
      missing,
      f"{what} is not a fully populated order entity; missing required "
      f"order properties {missing} (order.md: the payload is the same "
      "current-state snapshot as Get Order)",
    )
    try:
      order.Order(**payload)
    except ValidationError as error:
      self.fail(f"{what} is not a valid order entity: {error}")
    self.assertTrue(payload["line_items"], f"{what} carries no line_items")
    self.assertEqual(payload["id"], order_id)
    self.assertEqual(
      payload["checkout_id"],
      checkout_id,
      f"{what} does not reference the originating checkout",
    )
    self.assert_same_order_snapshot(order_id, payload, what)

  # ── order.md "Order Event Webhook": Standard Webhooks headers ───────────
  @no_rule(
    "order/index.md, Order Event Webhook: a 'Required Headers' table lists "
    "Webhook-Id and Webhook-Timestamp (unix), with no keyword"
  )
  def test_delivery_carries_standard_webhook_headers(self) -> None:
    """Every delivery carries Webhook-Id and a unix Webhook-Timestamp."""
    _, _, event = self._place_order()
    headers = event["headers"]
    self.assertTrue(
      headers.get("webhook-id"),
      "delivery is missing the required Webhook-Id header",
    )
    timestamp = headers.get("webhook-timestamp", "")
    self.assertRegex(
      timestamp,
      r"^\d+$",
      "Webhook-Timestamp must be a unix timestamp",
    )
    # The spec requires a unix timestamp but sets no skew bound; assert the
    # value parses into the unix-seconds era rather than inventing a window
    # (a millisecond value or a date string fails, clock skew does not).
    self.assertTrue(
      1_000_000_000 <= int(timestamp) <= 4_000_000_000,
      f"Webhook-Timestamp {timestamp} does not parse as unix seconds",
    )

  @spec_assert(
    "REQ-SHOPPING-ORDER-0018",  # retries keep Webhook-Id and Webhook-Timestamp
    "REQ-SHOPPING-ORDER-0025",  # MUST retry failed webhook deliveries
    "REQ-SHOPPING-ORDER-0026",  # same rule as 0018, in Business guidelines
  )
  def test_transient_failure_retries_preserve_event_identity(self) -> None:
    """A failed delivery is retried as the same webhook event."""
    self.webhook_server.failures_before_success = 1

    checkout_data = self.create_checkout_session(headers=self.get_headers())
    checkout_id = checkout_data["id"]
    complete_response = self.complete_checkout_session(checkout_id)
    order_id = complete_response["order"]["id"]
    deliveries = self._wait_for_events(order_id, 2)

    self.assertGreaterEqual(
      len(deliveries),
      2,
      "failed webhook delivery was not retried (order.md: MUST retry failed "
      "webhook deliveries)",
    )
    self.assertEqual(deliveries[0]["response_status"], 500)
    self.assertEqual(deliveries[1]["response_status"], 200)
    self.assertTrue(
      deliveries[0]["headers"].get("webhook-id"),
      "delivery is missing the required Webhook-Id header",
    )
    self.assertEqual(
      len({delivery["headers"].get("webhook-id") for delivery in deliveries}),
      1,
      "retry attempts must preserve the Webhook-Id of the original event",
    )
    self.assertTrue(
      deliveries[0]["headers"].get("webhook-timestamp"),
      "delivery is missing the required Webhook-Timestamp header",
    )
    self.assertEqual(
      len(
        {
          delivery["headers"].get("webhook-timestamp")
          for delivery in deliveries
        }
      ),
      1,
      "retry attempts must preserve the Webhook-Timestamp of the original "
      "event",
    )
    self.assertTrue(
      all(
        delivery["payload"] == deliveries[0]["payload"]
        for delivery in deliveries
      ),
      "retry attempts must carry the same full order entity",
    )

  # ── order.md "Events": fully populated order entity ─────────────────────
  @spec_assert(
    "REQ-SHOPPING-ORDER-0008",  # full entity on every response, delivery too
    "REQ-SHOPPING-ORDER-0023",  # MUST send "Order created" with full entity
  )
  def test_order_created_event_is_full_order_entity(self) -> None:
    """The 'Order created' delivery body is the full order entity.

    The body validates against the order schema and is, member for member,
    the snapshot Get Order returns for the order: the same full entity on
    the asynchronous response as on the synchronous one (order.md
    Operations).
    """
    checkout_id, order_id, event = self._place_order()
    self._assert_full_order_entity(
      event["payload"], checkout_id, order_id, "the Order created delivery"
    )

  @spec_assert(
    "REQ-SHOPPING-ORDER-0008",  # full entity on every response, delivery too
    "REQ-SHOPPING-ORDER-0024",  # full order entity on updates, not deltas
  )
  def test_update_event_is_full_order_entity(self) -> None:
    """An update delivery is the full current-state snapshot, not a delta.

    After the order ships, the latest delivery reflects the shipment and is,
    member for member, the snapshot Get Order returns. (The first delivery
    predates the shipment, so it is the "Order created" test's to compare.)
    """
    checkout_id, order_id, _ = self._place_order()
    headers = self.get_headers()
    headers["Simulation-Secret"] = FLAGS.simulation_secret
    ship_response = self.client.post(
      f"/testing/simulate-shipping/{order_id}",
      headers=headers,
    )
    if ship_response.status_code == 403:
      self.skip_environment(
        "the simulation secret was rejected (/testing/simulate-shipping -> "
        "403); pass --simulation_secret matching the server under test"
      )
    if ship_response.status_code in (404, 405):
      self.skip_no_trigger(
        "a /testing/simulate-shipping/{order_id} hook that advances the "
        f"order (the server answered {ship_response.status_code})"
      )
    self.assert_response_status(ship_response, 200)
    events = self._wait_for_events(order_id, 2)
    self.assertGreaterEqual(
      len(events),
      2,
      "no update webhook delivered after the order shipped "
      "(order.md: MUST send full order entity on updates)",
    )
    update = events[-1]["payload"]
    self.assertTrue(
      any(
        fe.get("type") == "shipped"
        for fe in (update.get("fulfillment") or {}).get("events") or []
      ),
      "the latest delivery does not reflect the shipped state in its "
      "snapshot (order.md: updates carry the full current-state entity)",
    )
    self._assert_full_order_entity(
      update, checkout_id, order_id, "the update delivery"
    )

  # ── order.md "Webhook Signature Verification" ───────────────────────────
  def _signed_event(self) -> dict[str, Any]:
    """Place an order and return its delivery, skipping if unsigned.

    For the checks that read a signature's parts. A delivery with NONE of
    the signature headers means the server does not sign webhooks at all,
    which `test_signed_delivery_carries_required_headers_and_digest` fails
    (order.md: payloads MUST be signed); the checks that build on that skip
    rather than fail again on the same root cause.
    """
    _, _, event = self._place_order()
    if not any(event["headers"].get(h) for h in _SIGNATURE_HEADERS):
      self.skip_not_exercised(
        "the server sends unsigned webhook deliveries (no UCP-Agent, "
        "Signature, Signature-Input or Content-Digest header), which "
        "test_signed_delivery_carries_required_headers_and_digest fails"
      )
    return event

  @spec_assert(
    "REQ-SHOPPING-ORDER-0021",  # UCP-Agent header with the profile URL
    partly(
      "REQ-SHOPPING-ORDER-0022",  # sign payloads with RFC 9421 headers
      "the RFC 9421 headers, their shape and the Content-Digest; the "
      "signature value itself isn't verified against the business's keys",
    ),
  )
  def test_signed_delivery_carries_required_headers_and_digest(self) -> None:
    """A signed delivery carries the four headers in RFC 9421 shape.

    Checked: all four headers are present; ``UCP-Agent`` names the business
    profile; ``Content-Digest`` is the SHA-256 of the raw body; and
    ``Signature-Input`` / ``Signature`` are RFC 9421 dictionaries sharing a
    label, with a non-empty list of quoted component names, a ``keyid``
    (order.md verification step 1 reads it to pick the key) and a base64
    byte-sequence signature. Not checked: that the signature verifies.
    """
    _, _, event = self._place_order()
    headers = event["headers"]
    missing = [h for h in _SIGNATURE_HEADERS if not headers.get(h)]
    self.assertFalse(
      missing,
      f"signed delivery is missing required headers: {missing} "
      "(order.md Webhook Signature Verification)",
    )
    agent = headers["ucp-agent"]
    match = re.search(r'profile="([^"]+)"', agent)
    self.assertIsNotNone(
      match,
      f"UCP-Agent is not an RFC 8941 dictionary with profile=: {agent!r}",
    )
    profile_url = match.group(1).split("?")[0].rstrip("/")
    self.assertTrue(
      profile_url.endswith("/.well-known/ucp"),
      "UCP-Agent profile URL must point at the business's /.well-known/ucp "
      f"(signatures.md UCP-Agent parsing): {agent!r}",
    )
    digest_match = re.search(
      r"sha-256=:([A-Za-z0-9+/=]+):", headers["content-digest"]
    )
    self.assertIsNotNone(
      digest_match,
      f"Content-Digest is not sha-256 (RFC 9530): "
      f"{headers['content-digest']!r}",
    )
    expected = hashlib.sha256(event["raw_body"]).digest()
    self.assertEqual(
      base64.b64decode(digest_match.group(1)),
      expected,
      "Content-Digest does not match the SHA-256 of the raw delivery body",
    )
    self._assert_signature_shape(
      headers["signature-input"], headers["signature"]
    )

  def _assert_signature_shape(
    self, signature_input: str, signature: str
  ) -> None:
    """Assert the two signature headers have the RFC 9421 dictionary shape.

    Args:
      signature_input: The ``Signature-Input`` header value.
      signature: The ``Signature`` header value.

    """
    inputs = {
      m.group("label"): m
      for m in _SIGNATURE_INPUT_MEMBER.finditer(signature_input)
    }
    self.assertTrue(
      inputs,
      "Signature-Input is not an RFC 9421 dictionary of "
      f'label=("component" ...);params members: {signature_input!r}',
    )
    values = {
      m.group("label"): m.group("value")
      for m in _SIGNATURE_MEMBER.finditer(signature)
    }
    for label, member in inputs.items():
      components = member.group("components").strip()
      self.assertTrue(
        components and _COMPONENT_LIST.fullmatch(components),
        f"Signature-Input {label} must list quoted component names "
        f"(RFC 9421 §4.1): {signature_input!r}",
      )
      self.assertRegex(
        member.group("params"),
        r';keyid="[^"]+"',
        f"Signature-Input {label} carries no keyid, so a verifier cannot "
        "pick the key from the business profile's keys (order.md Webhook "
        f"Signature Verification step 1): {signature_input!r}",
      )
      self.assertIn(
        label,
        values,
        f"Signature has no member labelled {label} to match Signature-Input "
        f"(RFC 9421 §4.2): {signature!r}",
      )
      try:
        raw = base64.b64decode(values[label], validate=True)
      except (ValueError, binascii.Error):
        raw = b""
      self.assertTrue(
        raw,
        f"Signature {label} is not a base64 byte sequence (RFC 9421 §4.2 / "
        f"RFC 8941 §3.3.5): {signature!r}",
      )

  @older_version(
    "2026-04-08",
    "ucp#659 made webhook signatures also cover webhook-id and "
    "webhook-timestamp, and fixed the order example that the ucp-agent skip "
    "waits on; the rule is in signatures.md, outside the catalog",
  )
  def test_signature_covers_ucp_agent(self) -> None:
    """The signed components include ucp-agent (binds signer identity).

    signatures.md's REST signing component table requires ``ucp-agent``
    whenever the UCP-Agent header is present, and order.md requires that
    header on every webhook delivery — so a conformant webhook signature
    always covers it (along with the always-required derived components and
    the body components).
    """
    event = self._signed_event()
    signature_input = event["headers"].get("signature-input", "")
    match = re.search(r"\(([^)]*)\)", signature_input)
    self.assertIsNotNone(
      match,
      f"Signature-Input has no component list: {signature_input!r}",
    )
    components = re.findall(r'"([^"]+)"', match.group(1))
    for required in (
      "@method",
      "@authority",
      "@path",
      "content-digest",
      "content-type",
    ):
      self.assertIn(
        required,
        components,
        f"webhook signature does not cover {required!r} "
        f"(signatures.md signed-component table): {signature_input!r}",
      )
    # signatures.md's component table requires ucp-agent whenever the header
    # is present, but order.md's own example webhook signature omits it; that
    # inconsistency has a fix in flight (ucp#659). Until it lands, a merchant
    # following the example verbatim must not be failed here.
    if "ucp-agent" not in components:
      self.skipTest(
        "webhook signature does not cover ucp-agent; normative text requires "
        "it but the spec example omits it (fix pending in ucp#659) - not "
        "graded until the spec is self-consistent"
      )


if __name__ == "__main__":
  absltest.main()

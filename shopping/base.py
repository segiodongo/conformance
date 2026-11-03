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
from typing import Any, TypedDict
import uuid
from framework.base_test import (
  BaseIntegrationTest,
  flag_value,
)
from framework.decorators import requires_capability
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
  ) -> Any:
    """Create a checkout session and return the response JSON."""
    create_payload = self.create_checkout_payload(
      quantity=quantity,
      item_id=item_id,
      currency=currency,
      handlers=handlers,
      buyer=buyer,
      include_fulfillment=select_fulfillment,
    )

    request_headers = self.get_headers()
    if headers:
      request_headers.update(headers)

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=request_headers,
    )
    self.assert_response_status(response, [200, 201])
    checkout_data = response.json()

    if select_fulfillment:
      checkout_data = self.ensure_fulfillment_ready(checkout_data["id"])

    return checkout_data

  def ensure_fulfillment_ready(self, checkout_id: str) -> Any:
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
      )

    return checkout_data

  def complete_checkout_session(
    self, checkout_id: str, payment_payload: dict[str, Any] | None = None
  ) -> Any:
    """Complete a checkout session."""
    self.ensure_fulfillment_ready(checkout_id)

    if payment_payload is None:
      payment_payload = get_valid_payment_payload()

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=self.get_headers(),
    )
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
    return update_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )

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
      ),
      headers=request_headers,
    )
    self.assert_response_status(response, 200)
    return response.json()

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

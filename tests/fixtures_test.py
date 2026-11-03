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

"""Unit tests for the fixture triggers of the shopping tests.

`DynamicFixtureContext.trigger` reads a trigger key, `require_trigger` skips
when a server declares none, and `shopping/fixtures/README.md` documents
every key. The example tests are built inside the test methods and run
offline: no client or agent server is started.
"""

import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from absl.testing import flagsaver
from framework import base_test
from framework.base_test import BaseIntegrationTest
from framework.outcomes import (
  ERROR,
  NO_TRIGGER,
  PASSED,
  SKIPPED,
  RecordingResult,
)
from shopping.base import (
  DEFAULT_FIXTURE_CONFIG,
  DynamicFixtureContext,
  ShoppingIntegrationTestBase,
)

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "shopping" / "fixtures" / "README.md"
FLOWER_SHOP = json.loads(Path(DEFAULT_FIXTURE_CONFIG).read_text("utf-8"))


class TriggerTest(unittest.TestCase):
  """Tests for DynamicFixtureContext.TRIGGERS and trigger()."""

  def test_every_trigger_has_a_getter(self):
    """Test that each entry names a method of the context."""
    for key, getter in DynamicFixtureContext.TRIGGERS.items():
      with self.subTest(key=key):
        self.assertTrue(callable(getattr(DynamicFixtureContext, getter)))

  def test_flower_shop_declares_its_triggers(self):
    """Test that the sample's fixtures give its declared triggers a value."""
    context = DynamicFixtureContext(FLOWER_SHOP)
    declared = set(FLOWER_SHOP.get("test_fixtures", {})) & set(
      DynamicFixtureContext.TRIGGERS
    )
    self.assertTrue(declared)
    for key in DynamicFixtureContext.TRIGGERS:
      with self.subTest(key=key):
        if key in declared:
          self.assertIsNotNone(context.trigger(key))
        else:
          self.assertIsNone(context.trigger(key))
    self.assertEqual(context.trigger("valid_discount_code_2"), "WELCOME20")
    self.assertEqual(
      context.trigger("known_customer")["email"], "john.doe@example.com"
    )
    self.assertEqual(context.trigger("free_shipping_min_subtotal"), 10000)

  def test_all_registered_triggers_parse_valid_shapes(self):
    """Test that every registered trigger returns a value when well-formed."""
    merged = json.loads(json.dumps(FLOWER_SHOP))
    merged["test_fixtures"].update(
      {
        "catalog_product_with_options": {
          "id": "prod_abc123",
          "selected": [{"name": "Color", "label": "Blue"}],
        },
        "catalog_variant": {
          "product_id": "prod_abc123",
          "variant_id": "prod_abc123_size10",
        },
        "unit_price_item_sku": "cable_50m",
        "catalog_search_default_page_size": 10,
        "accepted_eligibility_claim": "com.example.student",
        "pricing_eligibility_claim": "com.example.loyalty_gold",
        "unverifiable_eligibility_claim": "com.example.unverified_employee",
      }
    )
    context = DynamicFixtureContext(merged)
    for key in DynamicFixtureContext.TRIGGERS:
      with self.subTest(key=key):
        self.assertIsNotNone(context.trigger(key))

  def test_undeclared_triggers_are_none(self):
    """Test that a server that declares nothing gets None for each."""
    for config in ({}, {"test_fixtures": {}}):
      context = DynamicFixtureContext(config)
      for key in DynamicFixtureContext.TRIGGERS:
        with self.subTest(key=key, config=config):
          self.assertIsNone(context.trigger(key))

  def test_malformed_triggers_are_none(self):
    """Test that a key without the shape its getter needs reads as absent."""
    context = DynamicFixtureContext(
      {
        "test_fixtures": {
          "known_customer": {"full_name": "No Email"},
          "known_customer_without_address": "jane@example.com",
          "dynamic_fulfillment": {
            "domestic": {
              "destination": {"address_country": "US"},
              "expected_option_id": "exp-ship-us",
            }
          },
          "catalog_product_with_options": {"id": "prod_only"},
          "catalog_variant": {"product_id": "prod_only"},
          "unit_price_item_sku": "   ",
          "catalog_search_default_page_size": True,
          "accepted_eligibility_claim": "not_reverse_domain",
          "pricing_eligibility_claim": "com..empty_segment",
          "unverifiable_eligibility_claim": 123,
        }
      }
    )
    for key in (
      "known_customer",
      "known_customer_without_address",
      "dynamic_fulfillment",
      "catalog_product_with_options",
      "catalog_variant",
      "unit_price_item_sku",
      "catalog_search_default_page_size",
      "accepted_eligibility_claim",
      "pricing_eligibility_claim",
      "unverifiable_eligibility_claim",
    ):
      with self.subTest(key=key):
        self.assertIsNone(context.trigger(key))

  def test_fallback_config(self):
    """Test that a trigger declared only in the fallback is found."""
    context = DynamicFixtureContext(
      {}, fallback_config={"test_fixtures": {"free_shipping_item_sku": "sku1"}}
    )
    self.assertEqual(context.trigger("free_shipping_item_sku"), "sku1")

  def test_unknown_name_is_an_error(self):
    """Test that asking for a key that isn't a trigger raises."""
    with self.assertRaises(KeyError):
      DynamicFixtureContext(FLOWER_SHOP).trigger("valid_item")


def run_shopping(case: type[unittest.TestCase], fixtures: dict) -> dict:
  """Run a shopping test class against a fixture file; outcomes by name.

  The server profile is left unset and its fetch fails at once, so no
  capability or version check applies, and the agent server isn't started.
  """
  with tempfile.TemporaryDirectory() as directory:
    config = Path(directory) / "test_fixtures.json"
    config.write_text(json.dumps(fixtures), encoding="utf-8")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
    with (
      flagsaver.flagsaver(
        server_url="http://127.0.0.1:9", fixture_config=str(config)
      ),
      mock.patch.object(BaseIntegrationTest, "server_profile", None),
      mock.patch.object(BaseIntegrationTest, "_start_agent_server"),
      mock.patch.object(
        base_test, "fetch_server_profile", side_effect=OSError("offline")
      ),
    ):
      result = unittest.TextTestRunner(
        stream=io.StringIO(), resultclass=RecordingResult
      ).run(suite)
  prefix = f"{case.__module__}.{case.__qualname__}."
  return {
    test_id.removeprefix(prefix): outcome
    for test_id, outcome in result.outcomes.items()
  }


class RequireTriggerTest(unittest.TestCase):
  """Tests for ShoppingIntegrationTestBase.require_trigger."""

  def test_returns_a_declared_trigger(self):
    """Test that a declared trigger's value comes back."""

    class Example(ShoppingIntegrationTestBase):
      def test_one(self):
        """Read the known customer."""
        customer = self.require_trigger("known_customer")
        self.assertEqual(customer["email"], "john.doe@example.com")

    outcomes = run_shopping(Example, FLOWER_SHOP)
    self.assertEqual(outcomes["test_one"].status, PASSED)

  def test_skips_naming_the_key(self):
    """Test the skip for a trigger the server doesn't declare."""

    class Example(ShoppingIntegrationTestBase):
      def test_one(self):
        """Never gets past the trigger."""
        self.require_trigger("known_customer")
        self.fail("ran without the trigger")

    outcome = run_shopping(Example, {"test_fixtures": {}})["test_one"]
    self.assertEqual(outcome.status, SKIPPED)
    self.assertEqual(outcome.category, NO_TRIGGER)
    self.assertEqual(outcome.detail, "known_customer")

  def test_unknown_trigger_is_a_test_error(self):
    """Test that a name that isn't a trigger errors rather than skips."""

    class Example(ShoppingIntegrationTestBase):
      def test_one(self):
        """Ask for a key that isn't a trigger."""
        self.require_trigger("valid_item")

    outcome = run_shopping(Example, FLOWER_SHOP)["test_one"]
    self.assertEqual(outcome.status, ERROR)
    self.assertIn("is not a trigger", outcome.detail)


class ReadmeTest(unittest.TestCase):
  """Tests that the fixtures README and the code agree."""

  def test_readme_lists_every_trigger(self):
    """Test that each trigger key has a row in the README."""
    text = README.read_text(encoding="utf-8")
    for key in DynamicFixtureContext.TRIGGERS:
      with self.subTest(key=key):
        self.assertIn(f"| `{key}`", text)

  def test_tests_ask_only_for_registered_triggers(self):
    """Test that every require_trigger call in the suites names a trigger."""
    asked = set()
    for path in (ROOT / "shopping").rglob("*_test.py"):
      asked.update(
        re.findall(r'require_trigger\("([^"]+)"\)', path.read_text("utf-8"))
      )
    self.assertTrue(asked)
    self.assertLessEqual(asked, set(DynamicFixtureContext.TRIGGERS))

  def test_readme_names_existing_tests(self):
    """Test that the tests the trigger table names exist."""
    text = README.read_text(encoding="utf-8")
    table = text.split("## Triggers")[1].split("## Keys every server")[0]
    named = set(re.findall(r"`(\w+_test)\.(test_\w+)`", table))
    self.assertTrue(named)
    for module, method in sorted(named):
      with self.subTest(test=f"{module}.{method}"):
        matches = list((ROOT / "shopping").rglob(f"{module}.py"))
        self.assertEqual(len(matches), 1, module)
        self.assertIn(f"def {method}(", matches[0].read_text("utf-8"))


if __name__ == "__main__":
  unittest.main()

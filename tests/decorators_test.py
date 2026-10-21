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

"""Unit tests for the specification markers in framework.decorators."""

import unittest

from framework.decorators import (
  SpecMarker,
  no_rule,
  scenario,
  spec_assert,
  spec_marker,
)

RULE = "REQ-SHOPPING-CHECKOUT-0133"
OTHER_RULE = "REQ-SHOPPING-CHECKOUT-0134"


class SpecAssertTest(unittest.TestCase):
  """Tests for spec_assert."""

  def test_records_rule_ids_in_order(self):
    """Test that the marker keeps the rule IDs in the order given."""

    @spec_assert(OTHER_RULE, RULE)
    def test_method():
      pass

    self.assertEqual(
      spec_marker(test_method),
      SpecMarker("spec_assert", rule_ids=(OTHER_RULE, RULE)),
    )

  def test_returns_the_same_function(self):
    """Test that the marker labels the test without wrapping it."""

    def test_method():
      return "ran"

    self.assertIs(spec_assert(RULE)(test_method), test_method)
    self.assertEqual(test_method(), "ran")

  def test_rejects_bad_ids(self):
    """Test that a missing, malformed or repeated ID fails at once."""
    for rule_ids in (
      (),
      ("REQ-SHOPPING-CHECKOUT",),
      ("req-shopping-checkout-0133",),
      ("Totals", RULE),
      (133,),
      (RULE, RULE),
    ):
      with self.subTest(rule_ids=rule_ids), self.assertRaises(ValueError):
        spec_assert(*rule_ids)


class NoRuleAndScenarioTest(unittest.TestCase):
  """Tests for no_rule and scenario."""

  def test_records_the_reason(self):
    """Test that each marker keeps its kind and its trimmed reason."""
    for marker, kind in ((no_rule, "no_rule"), (scenario, "scenario")):
      with self.subTest(kind=kind):

        @marker("  described in discount.md without a keyword ")
        def test_method():
          pass

        self.assertEqual(
          spec_marker(test_method),
          SpecMarker(kind, reason="described in discount.md without a keyword"),
        )

  def test_rejects_an_empty_reason(self):
    """Test that a marker without a reason fails at once."""
    for marker in (no_rule, scenario):
      for reason in ("", "   ", None):
        with (
          self.subTest(marker=marker.__name__, reason=reason),
          self.assertRaises(ValueError),
        ):
          marker(reason)


class LabellingTest(unittest.TestCase):
  """Tests that hold for every marker."""

  def test_one_marker_per_test(self):
    """Test that a second marker on the same test fails at once."""

    @spec_assert(RULE)
    def test_method():
      pass

    for second in (
      spec_assert(OTHER_RULE),
      no_rule("a reason"),
      scenario("a reason"),
    ):
      with self.subTest(second=second), self.assertRaises(ValueError):
        second(test_method)

  def test_labels_methods_only(self):
    """Test that a marker on a class fails: each test cites its own rules."""
    with self.assertRaises(TypeError):

      @spec_assert(RULE)
      class Marked(unittest.TestCase):
        pass

  def test_unmarked_test_has_no_marker(self):
    """Test that spec_marker returns None for a test without a marker."""

    def test_method():
      pass

    self.assertIsNone(spec_marker(test_method))

  def test_marked_tests_run_as_before(self):
    """Test that unittest runs marked tests unchanged, markers readable."""

    class Example(unittest.TestCase):
      @spec_assert(RULE)
      def test_passes(self):
        """Pass."""

      @no_rule("described without a keyword")
      def test_fails(self):
        """Fail."""
        self.fail("fails as written")

    loaded = list(unittest.defaultTestLoader.loadTestsFromTestCase(Example))
    result = unittest.TestResult()
    unittest.TestSuite(loaded).run(result)
    self.assertEqual((result.testsRun, len(result.failures)), (2, 1))
    markers = {test._testMethodName: spec_marker(test) for test in loaded}
    self.assertEqual(markers["test_passes"].rule_ids, (RULE,))
    self.assertEqual(markers["test_fails"].kind, "no_rule")


if __name__ == "__main__":
  unittest.main()

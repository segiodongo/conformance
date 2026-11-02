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

"""Unit tests for recording test outcomes in framework.outcomes.

Each test builds its example test cases inside a function, so that neither
pytest nor unittest collects them on their own.
"""

import io
import unittest

from framework import outcomes
from framework.outcomes import (
  ERROR,
  FAILED,
  MAX_DETAIL,
  NOT_APPLICABLE,
  NOT_APPLICABLE_PREFIX,
  NOT_RUN,
  PASSED,
  SKIPPED,
  Outcome,
  RecordingResult,
)


def run(*cases: type[unittest.TestCase]) -> RecordingResult:
  """Run test case classes quietly; return the recording result."""
  suite = unittest.TestSuite(
    unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in cases
  )
  runner = unittest.TextTestRunner(
    stream=io.StringIO(), resultclass=RecordingResult
  )
  return runner.run(suite)


def name(case: type[unittest.TestCase], method: str) -> str:
  """Return the ID unittest gives a test."""
  return f"{case.__module__}.{case.__qualname__}.{method}"


class RecordingResultTest(unittest.TestCase):
  """Tests for RecordingResult."""

  def test_each_outcome(self):
    """Test passes, failures, errors and skips, with their details."""

    class Example(unittest.TestCase):
      def test_pass(self):
        """Pass."""

      def test_fail(self):
        """Fail an assertion with a message over several lines."""
        self.assertEqual(404, 200, "\n\ncheckout not created\nbody: {}")

      def test_error(self):
        """Raise an error."""
        raise KeyError("totals")

      def test_bare_error(self):
        """Raise an error with no message."""
        raise RuntimeError

      def test_skip(self):
        """Skip."""
        self.skipTest("no second discount code configured")

      @unittest.expectedFailure
      def test_expected_failure(self):
        """Fail as expected."""
        self.fail("known gap")

      @unittest.expectedFailure
      def test_unexpected_success(self):
        """Pass unexpectedly."""

      def test_not_applicable(self):
        """Skip because the server's version doesn't state the rule."""
        self.skipTest(
          f"{NOT_APPLICABLE_PREFIX} the server implements 2026-04-08, which "
          "doesn't state REQ-SHOPPING-CHECKOUT-0016 (introduced in 2026-08-25)"
        )

    result = run(Example)
    self.assertEqual(
      result.outcomes,
      {
        name(Example, "test_pass"): Outcome(PASSED),
        name(Example, "test_fail"): Outcome(
          FAILED, "404 != 200 : checkout not created body: {}"
        ),
        name(Example, "test_error"): Outcome(ERROR, "KeyError: 'totals'"),
        name(Example, "test_bare_error"): Outcome(ERROR, "RuntimeError"),
        name(Example, "test_skip"): Outcome(
          SKIPPED, "no second discount code configured"
        ),
        name(Example, "test_expected_failure"): Outcome(
          FAILED, "expected failure: known gap"
        ),
        name(Example, "test_unexpected_success"): Outcome(
          PASSED, "unexpected success"
        ),
        name(Example, "test_not_applicable"): Outcome(
          NOT_APPLICABLE,
          "the server implements 2026-04-08, which doesn't state "
          "REQ-SHOPPING-CHECKOUT-0016 (introduced in 2026-08-25)",
        ),
      },
    )
    self.assertEqual((len(result.failures), len(result.errors)), (1, 2))
    # unittest counts a not-applicable test among the skipped.
    self.assertEqual(len(result.skipped), 2)
    self.assertEqual(result.testsRun, 8)

  def test_subtests(self):
    """Test that a failing or skipped subtest counts for its test."""

    class Example(unittest.TestCase):
      def test_fails_once(self):
        """Fail the second of three subtests."""
        for value in range(3):
          with self.subTest(value=value):
            self.assertNotEqual(value, 1)

      def test_errs_then_fails(self):
        """Raise in one subtest, then fail in another."""
        with self.subTest("first"):
          raise ValueError("bad total")
        with self.subTest("second"):
          self.fail("never kept")

      def test_skips_one(self):
        """Skip one subtest and pass the other."""
        with self.subTest("skipped"):
          self.skipTest("no fixture")
        with self.subTest("passed"):
          pass

    result = run(Example)
    self.assertEqual(
      result.outcomes[name(Example, "test_fails_once")],
      Outcome(FAILED, "(value=1): 1 == 1"),
    )
    self.assertEqual(
      result.outcomes[name(Example, "test_errs_then_fails")],
      Outcome(ERROR, "[first]: ValueError: bad total"),
    )
    self.assertEqual(
      result.outcomes[name(Example, "test_skips_one")],
      Outcome(SKIPPED, "no fixture"),
    )

  def test_class_fixtures(self):
    """Test the tests of a class whose setUpClass fails or skips."""

    class Broken(unittest.TestCase):
      @classmethod
      def setUpClass(cls):
        """Fail before any test runs."""
        raise ConnectionError("server unreachable")

      def test_one(self):
        """Never run."""

    class Skipped(unittest.TestCase):
      @classmethod
      def setUpClass(cls):
        """Skip the whole class."""
        raise unittest.SkipTest("no order capability")

      def test_one(self):
        """Never run."""

    result = run(Broken, Skipped)
    self.assertEqual(result.outcomes, {})
    found = result.outcomes_for(
      [name(Broken, "test_one"), name(Skipped, "test_one"), "other.Test.test"]
    )
    self.assertEqual(
      list(found.values()),
      [
        Outcome(
          NOT_RUN, "setUpClass failed: ConnectionError: server unreachable"
        ),
        Outcome(SKIPPED, "no order capability"),
        Outcome(NOT_RUN, "no outcome was recorded"),
      ],
    )

  def test_outcomes_for_keeps_recorded_outcomes(self):
    """Test that outcomes_for returns what the run recorded, in order."""

    class Example(unittest.TestCase):
      def test_pass(self):
        """Pass."""

    result = run(Example)
    found = result.outcomes_for(
      ["missing.Test.test", name(Example, "test_pass")]
    )
    self.assertEqual(
      list(found.items()),
      [
        ("missing.Test.test", Outcome(NOT_RUN, "no outcome was recorded")),
        (name(Example, "test_pass"), Outcome(PASSED)),
      ],
    )

  def test_long_details_are_shortened(self):
    """Test that a detail is cut to MAX_DETAIL characters."""

    class Example(unittest.TestCase):
      def test_long(self):
        """Fail with a long message."""
        self.fail("x" * 500)

    detail = run(Example).outcomes[name(Example, "test_long")].detail
    self.assertEqual(len(detail), MAX_DETAIL)
    self.assertTrue(detail.endswith("…"))
    self.assertEqual(outcomes.STATUSES[-1], NOT_RUN)


if __name__ == "__main__":
  unittest.main()

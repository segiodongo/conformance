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

"""Unit tests for the runner's console summary.

The summary counts one outcome per test, as the requirements report does;
unittest's own `failures` list has one entry per failing subtest, which
used to make the console show one more failure and one fewer pass than
`matrix.json` whenever a test failed two subtests.
"""

import contextlib
import io
import os
import re
import unittest
from unittest import mock

from absl.testing import flagsaver
import runner


def run_suite(suite: unittest.TestSuite) -> str:
  """Run `suite` through the runner's main and return its console output."""
  out = io.StringIO()
  fetch = mock.Mock(side_effect=OSError("offline"))
  with (
    flagsaver.flagsaver(),
    mock.patch.dict(os.environ),
    mock.patch.object(runner, "fetch_server_profile", fetch),
    mock.patch.object(runner, "_load_tests", return_value=(suite, [])),
    contextlib.redirect_stdout(out),
    contextlib.redirect_stderr(out),
  ):
    runner.main(["--server_url=https://shop.example"])
  return out.getvalue()


def summary(output: str) -> dict[str, int]:
  """Return the counts the summary printed, by label."""
  counts = {}
  for label in ("Total Executed", "Passed", "Skipped", "Failures", "Errors"):
    match = re.search(rf"^{label}: (\d+)$", output, re.MULTILINE)
    counts[label] = int(match.group(1)) if match else None
  return counts


class SummaryCountsTest(unittest.TestCase):
  """The console summary counts per test."""

  def test_two_failing_subtests_are_one_failure(self):
    """A test with two failing subtests counts once; the pass is kept."""

    class Case(unittest.TestCase):
      def test_two_subtests_fail(self):
        for part in (1, 2):
          with self.subTest(part=part):
            self.fail(f"part {part}")

      def test_passes(self):
        pass

      def test_skips(self):
        self.skipTest("no trigger declared: something")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Case)
    output = run_suite(suite)

    self.assertEqual(
      summary(output),
      {
        "Total Executed": 3,
        "Passed": 1,
        "Skipped": 1,
        "Failures": 1,
        "Errors": 0,
      },
      output,
    )
    self.assertIn("by reason: no trigger declared 1", output)

  def test_error_and_not_applicable(self):
    """An error counts as an error; a not-applicable skip is set apart."""

    class Case(unittest.TestCase):
      def test_errors(self):
        raise KeyError("order")

      def test_not_applicable(self):
        self.skipTest("not applicable: the rule is from a later version")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Case)
    output = run_suite(suite)

    self.assertEqual(
      summary(output),
      {
        "Total Executed": 2,
        "Passed": 0,
        "Skipped": 0,
        "Failures": 0,
        "Errors": 1,
      },
      output,
    )
    self.assertIn("Not applicable: 1", output)


if __name__ == "__main__":
  unittest.main()

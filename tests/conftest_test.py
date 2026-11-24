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

"""Unit tests for the unit tests' own pytest configuration.

`tests/conftest.py` reports a unit test that skipped as failed. The change
to a report is checked on reports built by hand; that pytest then fails the
run is checked by running it, on a folder that holds a copy of the
configuration and a few example tests.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

from tests import conftest

# Unit tests the way the ones here are written: a suite call that skips
# where a pass was expected, one inside a subTest, and one that passes.
EXAMPLES = """
import unittest


def suite_call(skip):
  if skip:
    raise unittest.SkipTest("no trigger declared: a hook")


class ExampleTest(unittest.TestCase):
  def test_the_suite_call_skips(self):
    suite_call(skip=True)

  def test_the_suite_call_skips_in_a_subtest(self):
    for skip in (False, True):
      with self.subTest(skip=skip):
        suite_call(skip)

  def test_the_suite_call_passes(self):
    suite_call(skip=False)
"""


def report(outcome: str, longrepr=None, **extra) -> types.SimpleNamespace:
  """Build what `skip_as_failure` reads of a pytest report."""
  return types.SimpleNamespace(
    outcome=outcome,
    skipped=outcome == "skipped",
    longrepr=longrepr,
    **extra,
  )


class SkipAsFailureTest(unittest.TestCase):
  """`skip_as_failure` rewrites the report of a skipped test."""

  def test_a_skip_becomes_a_failure_that_keeps_the_reason(self):
    """The reason is the one the code under test skipped with."""
    skipped = report(
      "skipped",
      ("tests/example_test.py", 12, "Skipped: no trigger declared: a hook"),
    )

    self.assertIs(conftest.skip_as_failure(skipped), skipped)

    self.assertEqual(skipped.outcome, "failed")
    self.assertIn("A unit test here never skips.", skipped.longrepr)
    self.assertIn("Skipped: no trigger declared: a hook", skipped.longrepr)

  def test_a_skip_reported_as_text_becomes_a_failure_too(self):
    """Some reports carry the reason as it is, not in a location tuple."""
    skipped = report("skipped", "Skipped: environment: no server")

    conftest.skip_as_failure(skipped)

    self.assertEqual(skipped.outcome, "failed")
    self.assertIn("Skipped: environment: no server", skipped.longrepr)

  def test_other_outcomes_are_left_alone(self):
    """A pass, a failure and an expected failure stay what they are."""
    for outcome, extra in (
      ("passed", {}),
      ("failed", {}),
      ("skipped", {"wasxfail": "known problem"}),
    ):
      with self.subTest(outcome=outcome, extra=extra):
        untouched = report(outcome, "as reported", **extra)

        conftest.skip_as_failure(untouched)

        self.assertEqual(untouched.outcome, outcome)
        self.assertEqual(untouched.longrepr, "as reported")


class SkippedUnitTestFailsTheRunTest(unittest.TestCase):
  """Pytest, run on example tests, with and without the configuration."""

  def _pytest(self, with_conftest: bool) -> subprocess.CompletedProcess:
    """Run pytest on the examples in a folder of their own."""
    folder = Path(tempfile.mkdtemp())
    self.addCleanup(shutil.rmtree, folder)
    (folder / "example_test.py").write_text(EXAMPLES, encoding="utf-8")
    # A configuration file of the folder's own, so that pytest takes the
    # folder for the project and reads no other configuration on the way up.
    (folder / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    if with_conftest:
      shutil.copy(conftest.__file__, folder / "conftest.py")
    return subprocess.run(
      [sys.executable, "-m", "pytest", "-q", str(folder)],
      cwd=folder,
      env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
      capture_output=True,
      text=True,
      check=False,
    )

  def test_the_run_fails_and_says_why(self):
    """The exit status is what a caller reads, so it is what is checked."""
    run = self._pytest(with_conftest=True)

    self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
    self.assertIn("A unit test here never skips.", run.stdout)
    self.assertIn("Skipped: no trigger declared: a hook", run.stdout)
    # The test that skipped and the subtest that did; the other subtest,
    # its test and the third test passed.
    summary = run.stdout.splitlines()[-1]
    self.assertIn("2 failed, 2 passed", summary)
    self.assertNotIn("skipped", summary)
    self.assertIn("ExampleTest::test_the_suite_call_skips ", run.stdout)
    self.assertIn(
      "(skip=True) "
      "example_test.py::ExampleTest::test_the_suite_call_skips_in_a_subtest",
      run.stdout,
    )

  def test_without_the_configuration_the_same_run_succeeds(self):
    """Why the configuration is there: pytest alone calls this a success."""
    run = self._pytest(with_conftest=False)

    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
    self.assertIn("2 passed, 2 skipped", run.stdout.splitlines()[-1])


if __name__ == "__main__":
  unittest.main()

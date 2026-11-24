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

"""Pytest configuration for the unit tests.

Many unit tests call a suite test, or one of its helpers, and expect it to
pass or to fail. A suite test that skips instead raises `unittest.SkipTest`,
which leaves the unit test and makes pytest report the unit test itself as
skipped: neither passed nor failed, so a failure that had become a skip
would go unnoticed. No unit test here skips on purpose, so a skipped one is
reported as failed, with the reason the code under test gave.
"""

from collections.abc import Generator

import pytest


def skip_as_failure(report: pytest.TestReport) -> pytest.TestReport:
  """Turn a report of a skipped unit test into one of a failed test.

  Args:
    report: A report as pytest made it, of a test or of a subtest.

  Returns:
    The same report: failed, with the skip's reason, if it was skipped;
    untouched otherwise, and when the skip is an expected failure.

  """
  if not report.skipped or hasattr(report, "wasxfail"):
    return report
  reason = (
    report.longrepr[2]
    if isinstance(report.longrepr, tuple)
    else report.longrepr
  )
  report.outcome = "failed"
  report.longrepr = (
    "The code under test skipped where the unit test expected it to pass "
    f"or to fail. A unit test here never skips.\n{reason}"
  )
  return report


# The report is changed where it is made, before pytest counts it: the
# session decides the run's exit status from the reports it is shown, and it
# is shown them before a conftest's own report hook is. Outermost, so that
# another plugin's mark of an expected failure is already on the report.
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport() -> Generator[
  None, pytest.TestReport, pytest.TestReport
]:
  """Report a unit test, or a subtest, that skipped as failed."""
  return skip_as_failure((yield))

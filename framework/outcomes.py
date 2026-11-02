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

"""Record what happened to each test in a unittest run.

Demo only. The runner records each test's outcome so that the requirements
report can say, for each rule, whether the tests that cite it passed. A test
passes, fails an assertion, stops on an error, or is skipped. A skip whose
reason starts with `NOT_APPLICABLE_PREFIX` says the version the server
implements states none of the rules the test cites (see
`framework.versions`); it is recorded as not applicable, an outcome of its
own that never counts against the server. A test with no outcome at the end
of the run didn't run, for example because its class's `setUpClass` failed.

Every other skip says why, with a reason that starts with one of
`SKIP_PREFIXES`; the start names the skip's category, which the runner's
summary and the report count. A test's base class has a method per category
(`skip_no_trigger` and so on) that writes the prefix.
"""

from collections.abc import Iterable
import dataclasses
import re
import unittest

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
NOT_APPLICABLE = "not_applicable"
NOT_RUN = "not_run"
# Every status, in the order the report lists them.
STATUSES = (PASSED, FAILED, ERROR, SKIPPED, NOT_APPLICABLE, NOT_RUN)
# How a skip reason begins when the test doesn't apply to the server's version.
NOT_APPLICABLE_PREFIX = "not applicable:"
# Why a test was skipped, or didn't run. The fixtures declare no trigger for
# what the test needs; the run's own settings or surroundings stop it; the
# server's responses gave it nothing to check; the server doesn't advertise a
# capability it requires; or something else, such as a check on hold for a
# specification fix, or a fixture that failed before the test started.
NO_TRIGGER = "no_trigger"
ENVIRONMENT = "environment"
NOT_EXERCISED = "not_exercised"
CAPABILITY = "capability"
OTHER = "other"
# Every category, in the order the report lists them.
SKIP_CATEGORIES = (NO_TRIGGER, ENVIRONMENT, NOT_EXERCISED, CAPABILITY, OTHER)
# How a skip reason begins for each category that has a prefix.
SKIP_PREFIXES = {
  NO_TRIGGER: "no trigger declared:",
  ENVIRONMENT: "environment:",
  NOT_EXERCISED: "not exercised:",
  CAPABILITY: "capability not advertised:",
}
# How the runner and the report name each category.
SKIP_LABELS = {
  NO_TRIGGER: "no trigger declared",
  ENVIRONMENT: "environment",
  NOT_EXERCISED: "not exercised",
  CAPABILITY: "capability not advertised",
  OTHER: "other reason",
}
# The longest detail kept, in characters.
MAX_DETAIL = 200
# How unittest names a class or module fixture that failed or skipped, as in
# "setUpClass (shopping.checkout.example_test.ExampleTest)".
_FIXTURE = re.compile(r"(\w+) \((.+)\)")


def is_not_applicable(reason: str) -> bool:
  """Return whether a skip reason says the test doesn't apply to the server."""
  return reason.startswith(NOT_APPLICABLE_PREFIX)


def skip_reason(category: str, text: str) -> str:
  """Return a skip reason that starts with a category's prefix.

  Args:
    category: One of `SKIP_PREFIXES`' keys.
    text: What the test needed, or what stopped it.

  Returns:
    The reason, such as "no trigger declared: a known customer".

  """
  return f"{SKIP_PREFIXES[category]} {text}"


def skip_category(reason: str) -> tuple[str, str]:
  """Return a skip reason's category, and its text without the prefix.

  Args:
    reason: The reason a test was skipped with.

  Returns:
    One of `SKIP_CATEGORIES`, and the reason's text after the category's
    prefix: the whole reason when it has none, which makes it `OTHER`.

  """
  for category, prefix in SKIP_PREFIXES.items():
    if reason.startswith(prefix):
      return category, reason.removeprefix(prefix).strip()
  return OTHER, reason


@dataclasses.dataclass(frozen=True)
class Outcome:
  """What happened to one test.

  Attributes:
    status: One of `STATUSES`.
    detail: Why the test was skipped or didn't run, or its failure or error
      message, on one line and shortened.
    category: For a skipped test or one that didn't run, why, as one of
      `SKIP_CATEGORIES`; empty for other outcomes, and for a skip recorded
      without one, which counts as `OTHER`.

  """

  status: str
  detail: str = ""
  category: str = ""


class RecordingResult(unittest.TextTestResult):
  """A text test result that also records each test's outcome by ID.

  The `add...` methods keep unittest's names, and print what
  `unittest.TextTestResult` prints.

  Attributes:
    outcomes: The outcome of each test that started, by test ID.
    fixtures: For each class or module whose fixture failed or skipped, by
      its dotted name, the outcome its tests take.

  """

  def __init__(self, *args, **kwargs):
    """Start with no outcomes; the arguments are TextTestResult's."""
    super().__init__(*args, **kwargs)
    self.outcomes: dict[str, Outcome] = {}
    self.fixtures: dict[str, Outcome] = {}

  def addSuccess(self, test):  # noqa: N802
    """Record a pass."""
    super().addSuccess(test)
    self._record(test, Outcome(PASSED))

  def addFailure(self, test, err):  # noqa: N802
    """Record a failed assertion."""
    super().addFailure(test, err)
    self._record(test, Outcome(FAILED, _message(err)))

  def addError(self, test, err):  # noqa: N802
    """Record an error."""
    super().addError(test, err)
    self._record(test, Outcome(ERROR, _message(err, with_type=True)))

  def addSkip(self, test, reason):  # noqa: N802
    """Record a skip with its category; a not-applicable one on its own."""
    super().addSkip(test, reason)
    if is_not_applicable(reason):
      detail = reason.removeprefix(NOT_APPLICABLE_PREFIX)
      self._record(test, Outcome(NOT_APPLICABLE, _shorten(detail)))
    else:
      category, detail = skip_category(reason)
      self._record(test, Outcome(SKIPPED, _shorten(detail), category))

  def addExpectedFailure(self, test, err):  # noqa: N802
    """Record an expected failure as a failure: the check didn't pass."""
    super().addExpectedFailure(test, err)
    detail = _shorten(f"expected failure: {_message(err)}")
    self._record(test, Outcome(FAILED, detail))

  def addUnexpectedSuccess(self, test):  # noqa: N802
    """Record an unexpected success as a pass: the check passed."""
    super().addUnexpectedSuccess(test)
    self._record(test, Outcome(PASSED, "unexpected success"))

  def addSubTest(self, test, subtest, err):  # noqa: N802
    """Record a failing subtest as its test's failure or error."""
    super().addSubTest(test, subtest, err)
    if err is None:
      return
    where = subtest.id().removeprefix(test.id()).strip()
    if issubclass(err[0], test.failureException):
      outcome = Outcome(FAILED, _shorten(f"{where}: {_message(err)}"))
    else:
      detail = _message(err, with_type=True)
      outcome = Outcome(ERROR, _shorten(f"{where}: {detail}"))
    self._record(test, outcome)

  def outcomes_for(self, test_ids: Iterable[str]) -> dict[str, Outcome]:
    """Return the outcome of each of the given tests.

    A test that never started didn't run. If its class's or module's fixture
    failed, the fixture's error is the reason; if the fixture skipped, the
    test was skipped too.

    Args:
      test_ids: The IDs of the tests the run selected.

    Returns:
      Each test's outcome, by ID.

    """
    return {
      test_id: self.outcomes.get(test_id) or self._not_started(test_id)
      for test_id in test_ids
    }

  def _not_started(self, test_id: str) -> Outcome:
    """Return the outcome of a test that never started."""
    class_name = test_id.rpartition(".")[0]
    module = class_name.rpartition(".")[0]
    for parent in (class_name, module):
      if parent in self.fixtures:
        return self.fixtures[parent]
    return Outcome(NOT_RUN, "no outcome was recorded", OTHER)

  def _record(self, test: object, outcome: Outcome) -> None:
    """Keep a test's first outcome, unless a failure or error follows."""
    if not isinstance(test, unittest.TestCase):
      self._record_fixture(test, outcome)
      return
    parent = getattr(test, "test_case", None)
    if isinstance(parent, unittest.TestCase):
      # A subtest's skip counts for the test it belongs to.
      test = parent
    test_id = test.id()
    earlier = self.outcomes.get(test_id)
    if earlier is None or (
      earlier.status in (PASSED, SKIPPED, NOT_APPLICABLE)
      and outcome.status in (FAILED, ERROR)
    ):
      self.outcomes[test_id] = outcome

  def _record_fixture(self, holder: object, outcome: Outcome) -> None:
    """Note a class or module fixture's error or skip, for its tests."""
    describe = getattr(holder, "id", None)
    match = _FIXTURE.fullmatch(describe()) if callable(describe) else None
    if not match:
      return
    fixture, parent = match.groups()
    if outcome.status not in (SKIPPED, NOT_APPLICABLE):
      outcome = Outcome(
        NOT_RUN, _shorten(f"{fixture} failed: {outcome.detail}"), OTHER
      )
    self.fixtures.setdefault(parent, outcome)


def _message(err: tuple, with_type: bool = False) -> str:
  """Return an exception's message on one line, shortened.

  Args:
    err: The exception, as `sys.exc_info()` gives it.
    with_type: Whether to start with the exception's type.

  Returns:
    The message.

  """
  exc_type, value = err[0], err[1]
  text = " ".join(str(value).split())
  if with_type:
    text = f"{exc_type.__name__}: {text}" if text else exc_type.__name__
  return _shorten(text)


def _shorten(text: str) -> str:
  """Return text on one line, cut to `MAX_DETAIL` characters."""
  text = " ".join(text.split())
  if len(text) <= MAX_DETAIL:
    return text
  return text[: MAX_DETAIL - 1].rstrip() + "…"

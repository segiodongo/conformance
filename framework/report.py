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

"""Build the requirements coverage report from the catalog and the markers.

Demo only. The runner writes the report with `--requirements_report=<dir>`.
It reads only the catalog and the tests' markers, so it needs no server and
works with `--dry-run`. For each capability, the report counts the rules,
the testable rules and the testable MUST and MUST NOT rules, and how many of
those at least one test cites. It also lists every rule with the tests that
cite it, and every shopping test with its marker.

A rule counts as covered when a test's `spec_assert` marker cites it: the
report says which rules have a test, not whether the tests pass.
"""

import collections
from collections.abc import Iterable, Mapping, Sequence
import dataclasses
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from framework.decorators import MARKER_KINDS
from framework.marker_check import LoadedTest, in_scope
from framework.requirements import Capability, Catalog, Rule, load_catalog

FORMAT_VERSION = 1
JSON_FILE = "matrix.json"
# The levels of an absolute requirement or prohibition.
MUST_LEVELS = frozenset({"MUST", "MUST NOT", "SHALL", "SHALL NOT"})
UNMARKED = "unmarked"


@dataclasses.dataclass(frozen=True)
class Tally:
  """Counts for a set of rules.

  Attributes:
    rules: How many rules there are.
    testable: How many of them this suite can test.
    testable_must: How many of the testable rules are MUST or MUST NOT.
    covered: How many testable rules at least one test cites.
    covered_must: How many testable MUST and MUST NOT rules at least one
      test cites.

  """

  rules: int
  testable: int
  testable_must: int
  covered: int
  covered_must: int

  @property
  def uncovered_must(self) -> int:
    """Return how many testable MUST and MUST NOT rules no test cites."""
    return self.testable_must - self.covered_must

  @property
  def must_coverage(self) -> float | None:
    """Return the share of testable MUST and MUST NOT rules a test cites."""
    if not self.testable_must:
      return None
    return self.covered_must / self.testable_must


@dataclasses.dataclass(frozen=True)
class CapabilityRow:
  """One capability's line in the report.

  Attributes:
    capability: The capability.
    tally: The counts for its rules.

  """

  capability: Capability
  tally: Tally


@dataclasses.dataclass(frozen=True)
class TestRow:
  """One shopping test in the report.

  Attributes:
    test_id: The test's ID, as the runner reports it.
    module: The module the test is defined in.
    kind: Its marker's kind, or "unmarked".
    rule_ids: The rules its `spec_assert` marker cites.
    reason: For the other kinds, its marker's reason.
    version: For `older_version`, the release the test checks.
    capabilities: The capabilities the test requires.

  """

  test_id: str
  module: str
  kind: str
  rule_ids: tuple[str, ...]
  reason: str
  version: str
  capabilities: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Report:
  """The coverage report.

  Attributes:
    spec_commit: The specification commit the catalog was built from.
    capabilities: One row per capability, in the catalog's order.
    total: The counts for all rules, each rule counted once.
    rules: Every rule, by ID. Sentences the review found are not rules are
      left out.
    cited_by: For each rule at least one test cites, those tests' IDs.
    tests: Every shopping test, by ID.
    not_rules: How many catalog sentences the review found are not rules.

  """

  spec_commit: str
  capabilities: tuple[CapabilityRow, ...]
  total: Tally
  rules: tuple[Rule, ...]
  cited_by: Mapping[str, tuple[str, ...]]
  tests: tuple[TestRow, ...]
  not_rules: int = 0

  def markers(self) -> dict[str, int]:
    """Return how many tests carry each kind of marker, or none."""
    counts = collections.Counter(test.kind for test in self.tests)
    return {kind: counts[kind] for kind in (*MARKER_KINDS, UNMARKED)}


def build(catalog: Catalog, tests: Iterable[LoadedTest]) -> Report:
  """Build the report for the shopping tests among the loaded tests.

  Args:
    catalog: The requirements catalog.
    tests: The loaded tests; those outside the shopping suite are ignored.

  Returns:
    The report.

  """
  rows = sorted(
    (_test_row(test) for test in tests if in_scope(test.module)),
    key=lambda row: row.test_id,
  )
  cited_by: dict[str, list[str]] = collections.defaultdict(list)
  for row in rows:
    for rule_id in dict.fromkeys(row.rule_ids):
      if catalog.status(rule_id) == "rule":
        cited_by[rule_id].append(row.test_id)
  rules = tuple(rule for rule in catalog.rules.values() if rule.is_rule)
  return Report(
    spec_commit=catalog.spec_commit,
    capabilities=tuple(
      CapabilityRow(
        capability,
        _tally(
          [rule for rule in rules if capability.name in rule.capabilities],
          cited_by,
        ),
      )
      for capability in _capabilities(catalog, rules)
    ),
    total=_tally(rules, cited_by),
    rules=rules,
    cited_by=MappingProxyType(
      {rule_id: tuple(ids) for rule_id, ids in sorted(cited_by.items())}
    ),
    tests=tuple(rows),
    not_rules=len(catalog.rules) - len(rules),
  )


def to_json(report: Report) -> dict[str, Any]:
  """Return the report as the object written to `matrix.json`."""
  return {
    "format": FORMAT_VERSION,
    "spec_commit": report.spec_commit,
    "totals": _tally_json(report.total),
    "not_rules": report.not_rules,
    "markers": report.markers(),
    "capabilities": [
      {
        "name": row.capability.name,
        "kind": row.capability.kind,
        "extends": list(row.capability.extends),
        **_tally_json(row.tally),
      }
      for row in report.capabilities
    ],
    "rules": [
      {
        "id": rule.id,
        "level": rule.level,
        "clause": rule.clause,
        "capabilities": list(rule.capabilities),
        "source_file": rule.source_file,
        "line": rule.line,
        "link": rule.link,
        "testable": rule.testable,
        "testable_reason": rule.testable_reason,
        "tests": list(report.cited_by.get(rule.id, ())),
      }
      for rule in report.rules
    ],
    "tests": [
      {
        "id": test.test_id,
        "module": test.module,
        "marker": test.kind,
        "rules": list(test.rule_ids),
        "reason": test.reason,
        "version": test.version,
        "capabilities": list(test.capabilities),
      }
      for test in report.tests
    ],
  }


def write(report: Report, directory: str | Path) -> list[Path]:
  """Write the report's files to a directory, creating it if need be.

  Args:
    report: The report.
    directory: Where to write it.

  Returns:
    The paths written.

  """
  out = Path(directory)
  out.mkdir(parents=True, exist_ok=True)
  path = out / JSON_FILE
  path.write_text(
    json.dumps(to_json(report), indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
  )
  return [path]


def summary(report: Report, paths: Iterable[Path]) -> str:
  """Return a few lines for the console about a written report."""
  total = report.total
  share = total.must_coverage
  commit = report.spec_commit[:8] or "an unknown commit"
  return (
    f"Requirements report: {', '.join(str(path) for path in paths)}\n"
    f"  catalog at {commit}: {total.rules} rules, {total.testable} "
    f"testable, {total.testable_must} of them MUST or MUST NOT\n"
    f"  {len(report.tests)} tests cite {len(report.cited_by)} rules; "
    f"{total.covered_must} of the {total.testable_must} testable MUST and "
    f"MUST NOT rules have a test"
    + (f" ({share:.0%})" if share is not None else "")
    + f", {total.uncovered_must} don't\n"
  )


def generate(
  tests: Iterable[LoadedTest],
  directory: str | Path,
  catalog_dir: str | Path | None = None,
) -> str:
  """Build the report for the loaded tests, write it, and describe it.

  Args:
    tests: The loaded tests.
    directory: Where to write the report.
    catalog_dir: The catalog directory; by default this repository's.

  Returns:
    A few lines for the console.

  Raises:
    CatalogError: If the catalog cannot be loaded.
    OSError: If the report cannot be written.

  """
  report = build(load_catalog(catalog_dir), tests)
  return summary(report, write(report, directory))


def _capabilities(catalog: Catalog, rules: Sequence[Rule]) -> list[Capability]:
  """Return the index's capabilities, then any other a rule belongs to."""
  listed = list(catalog.capabilities)
  names = {capability.name for capability in listed}
  others = {name for rule in rules for name in rule.capabilities} - names
  return listed + [Capability(name) for name in sorted(others)]


def _tally(rules: Sequence[Rule], cited: Mapping[str, object]) -> Tally:
  """Count rules, testable rules and testable MUSTs, and those cited."""
  testable = [rule for rule in rules if rule.testable]
  must = [rule for rule in testable if rule.level in MUST_LEVELS]
  return Tally(
    rules=len(rules),
    testable=len(testable),
    testable_must=len(must),
    covered=sum(rule.id in cited for rule in testable),
    covered_must=sum(rule.id in cited for rule in must),
  )


def _tally_json(tally: Tally) -> dict[str, int]:
  """Return a tally's counts as JSON, with the uncovered MUSTs."""
  return {
    "rules": tally.rules,
    "testable": tally.testable,
    "testable_must": tally.testable_must,
    "covered": tally.covered,
    "covered_must": tally.covered_must,
    "uncovered_must": tally.uncovered_must,
  }


def _test_row(test: LoadedTest) -> TestRow:
  """Describe a loaded test for the report."""
  marker = test.marker
  return TestRow(
    test_id=test.test_id,
    module=test.module,
    kind=marker.kind if marker else UNMARKED,
    rule_ids=marker.rule_ids if marker else (),
    reason=marker.reason if marker else "",
    version=marker.version if marker else "",
    capabilities=tuple(sorted(test.capabilities)),
  )

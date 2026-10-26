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

"""Unit tests for the coverage report in framework.report.

Most use a small catalog built in memory; the last classes read this
repository's catalog and drive the runner with `--dry-run`.
"""

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest
from unittest import mock

import runner
from framework import report
from framework.decorators import SpecMarker
from framework.marker_check import LoadedTest
from framework.requirements import (
  DEFAULT_CATALOG_DIR,
  INDEX_FILE,
  Capability,
  Catalog,
  Rule,
  load_catalog,
)

CART = "dev.ucp.shopping.cart"
CHECKOUT = "dev.ucp.shopping.checkout"
DISCOUNT = "dev.ucp.shopping.discount"
LOOKUP = "dev.ucp.shopping.catalog.lookup"
SEARCH = "dev.ucp.shopping.catalog.search"
OTHER = "dev.ucp.shopping.other"


def rule(
  rule_id: str,
  capabilities: tuple[str, ...],
  level: str = "MUST",
  testable: bool = True,
  is_rule: bool = True,
) -> Rule:
  """Return a rule."""
  return Rule(
    id=rule_id,
    level=level,
    clause=f"The business {level} do {rule_id}.",
    capabilities=capabilities,
    source_file="docs/specification/shopping/checkout/index.md",
    line=1,
    link="specification/shopping/checkout/#a",
    testable=testable,
    testable_reason="aimed at the business" if testable else "MCP page",
    is_rule=is_rule,
  )


RULES = (
  rule("REQ-SHOPPING-CART-0001", (CART,)),
  rule("REQ-SHOPPING-CATALOG-0001", (LOOKUP, SEARCH)),
  rule("REQ-SHOPPING-CHECKOUT-0001", (CHECKOUT,)),
  rule("REQ-SHOPPING-CHECKOUT-0002", (CHECKOUT,), level="MUST NOT"),
  rule("REQ-SHOPPING-CHECKOUT-0003", (CHECKOUT,), level="SHOULD"),
  rule("REQ-SHOPPING-CHECKOUT-0004", (CHECKOUT,), testable=False),
  rule("REQ-SHOPPING-CHECKOUT-0005", (CHECKOUT,), is_rule=False),
  rule("REQ-SHOPPING-OTHER-0001", (OTHER,), level="MAY"),
)
CATALOG = Catalog(
  rules=MappingProxyType({r.id: r for r in RULES}),
  retired=frozenset(),
  extends=MappingProxyType({DISCOUNT: (CART, CHECKOUT)}),
  capabilities=(
    Capability(CART),
    Capability(CHECKOUT),
    Capability(DISCOUNT, "extension", (CART, CHECKOUT)),
    Capability(LOOKUP),
    Capability(SEARCH),
  ),
  spec_commit="abc123def456",
)


def loaded(
  name: str,
  marker: SpecMarker | None,
  module: str = "shopping.checkout.example_test",
) -> LoadedTest:
  """Return a loaded test that requires checkout."""
  return LoadedTest(
    f"{module}.ExampleTest.{name}", module, marker, frozenset({CHECKOUT})
  )


def cites(*rule_ids: str) -> SpecMarker:
  """Return a spec_assert marker."""
  return SpecMarker("spec_assert", rule_ids=rule_ids)


TESTS = (
  loaded(
    "test_b",
    cites("REQ-SHOPPING-CHECKOUT-0001", "REQ-SHOPPING-CATALOG-0001"),
  ),
  loaded(
    "test_a", cites("REQ-SHOPPING-CHECKOUT-0001", "REQ-SHOPPING-CHECKOUT-0003")
  ),
  loaded("test_c", cites("REQ-SHOPPING-CHECKOUT-0004")),
  loaded(
    "test_d", cites("REQ-SHOPPING-CHECKOUT-0005", "REQ-SHOPPING-CHECKOUT-0099")
  ),
  loaded("test_e", SpecMarker("no_rule", reason="no keyword")),
  loaded("test_f", None),
  loaded("test_core", None, module="core.protocol_test"),
)


def full_id(name: str) -> str:
  """Return the ID `loaded` gives a test."""
  return f"shopping.checkout.example_test.ExampleTest.{name}"


class BuildTest(unittest.TestCase):
  """Tests for build."""

  def setUp(self):
    """Build the report for the small catalog."""
    super().setUp()
    self.report = report.build(CATALOG, TESTS)

  def test_tests_and_markers(self):
    """Test that shopping tests are listed by ID, and markers counted."""
    self.assertEqual(
      [row.test_id for row in self.report.tests],
      [full_id(f"test_{letter}") for letter in "abcdef"],
    )
    self.assertEqual(
      self.report.markers(),
      {
        "spec_assert": 4,
        "no_rule": 1,
        "scenario": 0,
        "outside_catalog": 0,
        "older_version": 0,
        "unmarked": 1,
      },
    )
    unmarked = self.report.tests[-1]
    self.assertEqual((unmarked.kind, unmarked.rule_ids), ("unmarked", ()))
    self.assertEqual(self.report.tests[4].reason, "no keyword")

  def test_cited_by(self):
    """Test that only IDs naming rules are cited, each with its tests."""
    self.assertEqual(
      dict(self.report.cited_by),
      {
        "REQ-SHOPPING-CATALOG-0001": (full_id("test_b"),),
        "REQ-SHOPPING-CHECKOUT-0001": (full_id("test_a"), full_id("test_b")),
        "REQ-SHOPPING-CHECKOUT-0003": (full_id("test_a"),),
        "REQ-SHOPPING-CHECKOUT-0004": (full_id("test_c"),),
      },
    )

  def test_totals(self):
    """Test the counts over all rules, each rule counted once."""
    total = self.report.total
    self.assertEqual(
      total,
      report.Tally(
        rules=7, testable=6, testable_must=4, covered=3, covered_must=2
      ),
    )
    self.assertEqual(total.uncovered_must, 2)
    self.assertEqual(total.must_coverage, 0.5)
    self.assertEqual(self.report.not_rules, 1)
    self.assertEqual(len(self.report.rules), 7)
    self.assertEqual(self.report.spec_commit, "abc123def456")

  def test_capabilities(self):
    """Test each capability's counts, in the index's order."""
    rows = {row.capability.name: row.tally for row in self.report.capabilities}
    self.assertEqual(
      [row.capability.name for row in self.report.capabilities],
      [CART, CHECKOUT, DISCOUNT, LOOKUP, SEARCH, OTHER],
    )
    self.assertEqual(rows[CHECKOUT], report.Tally(4, 3, 2, 2, 1))
    self.assertEqual(rows[CART], report.Tally(1, 1, 1, 0, 0))
    self.assertEqual(rows[LOOKUP], report.Tally(1, 1, 1, 1, 1))
    self.assertEqual(rows[SEARCH], rows[LOOKUP])
    self.assertEqual(rows[DISCOUNT], report.Tally(0, 0, 0, 0, 0))
    self.assertIsNone(rows[DISCOUNT].must_coverage)
    self.assertEqual(rows[OTHER], report.Tally(1, 1, 0, 0, 0))
    self.assertEqual(self.report.capabilities[2].capability.kind, "extension")


class OutputTest(unittest.TestCase):
  """Tests for to_json, write, summary and generate."""

  def setUp(self):
    """Build the report for the small catalog."""
    super().setUp()
    self.report = report.build(CATALOG, TESTS)

  def test_json(self):
    """Test the shape of matrix.json."""
    data = report.to_json(self.report)
    self.assertEqual(
      list(data),
      [
        "format",
        "spec_commit",
        "totals",
        "not_rules",
        "markers",
        "capabilities",
        "rules",
        "tests",
      ],
    )
    self.assertEqual(data["format"], report.FORMAT_VERSION)
    self.assertEqual(
      data["totals"],
      {
        "rules": 7,
        "testable": 6,
        "testable_must": 4,
        "covered": 3,
        "covered_must": 2,
        "uncovered_must": 2,
      },
    )
    discount = data["capabilities"][2]
    self.assertEqual(
      (discount["name"], discount["kind"], discount["extends"]),
      (DISCOUNT, "extension", [CART, CHECKOUT]),
    )
    by_id = {entry["id"]: entry for entry in data["rules"]}
    self.assertNotIn("REQ-SHOPPING-CHECKOUT-0005", by_id)
    self.assertEqual(
      by_id["REQ-SHOPPING-CHECKOUT-0001"]["tests"],
      [full_id("test_a"), full_id("test_b")],
    )
    self.assertEqual(by_id["REQ-SHOPPING-CART-0001"]["tests"], [])
    self.assertEqual(
      data["tests"][3]["rules"],
      ["REQ-SHOPPING-CHECKOUT-0005", "REQ-SHOPPING-CHECKOUT-0099"],
    )
    self.assertEqual(data["tests"][0]["capabilities"], [CHECKOUT])

  def test_write(self):
    """Test that write creates the directory, and writes the same bytes."""
    with tempfile.TemporaryDirectory() as directory:
      out = Path(directory) / "new" / "reports"
      paths = report.write(self.report, out)
      self.assertEqual(paths, [out / report.JSON_FILE])
      first = paths[0].read_bytes()
      self.assertEqual(json.loads(first), report.to_json(self.report))
      report.write(self.report, out)
      self.assertEqual(paths[0].read_bytes(), first)

  def test_summary(self):
    """Test the console lines."""
    text = report.summary(self.report, [Path("out/matrix.json")])
    self.assertIn("Requirements report: out/matrix.json", text)
    self.assertIn(
      "catalog at abc123de: 7 rules, 6 testable, 4 of them MUST or MUST NOT",
      text,
    )
    self.assertIn(
      "6 tests cite 4 rules; 2 of the 4 testable MUST and MUST NOT rules "
      "have a test (50%), 2 don't",
      text,
    )

  def test_summary_without_musts(self):
    """Test the console lines when no rule is a testable MUST."""
    empty = Catalog(rules=MappingProxyType({}), retired=frozenset())
    text = report.summary(report.build(empty, ()), [])
    self.assertIn("catalog at an unknown commit: 0 rules", text)
    self.assertIn("have a test, 0 don't", text)


class RepositoryReportTest(unittest.TestCase):
  """Tests against this repository's catalog."""

  def test_counts_reconcile_with_the_index(self):
    """Test that the report's counts match capabilities.json."""
    built = report.build(load_catalog(), ())
    index = json.loads((DEFAULT_CATALOG_DIR / INDEX_FILE).read_text("utf-8"))
    expected = {
      entry["name"]: (
        entry["rules"],
        entry["testable_rules"],
        entry["testable_must"],
      )
      for entry in index["capabilities"]
    }
    self.assertEqual(
      {
        row.capability.name: (
          row.tally.rules,
          row.tally.testable,
          row.tally.testable_must,
        )
        for row in built.capabilities
      },
      expected,
    )
    self.assertEqual(
      (built.total.rules, built.total.testable, built.total.testable_must),
      (507, 217, 104),
    )
    self.assertEqual(built.not_rules, 3)
    self.assertTrue(built.spec_commit.startswith("f2626c28"))

  def test_generate(self):
    """Test that generate writes matrix.json and describes it."""
    with tempfile.TemporaryDirectory() as directory:
      text = report.generate((), directory)
      data = json.loads((Path(directory) / report.JSON_FILE).read_text())
    self.assertIn("507 rules, 217 testable", text)
    self.assertEqual(data["tests"], [])


class RunnerTest(unittest.TestCase):
  """Tests for the runner's --requirements_report option."""

  def run_main(self, *argv: str) -> tuple[int, str]:
    """Run the runner offline; return its exit code and its output."""
    out, err = io.StringIO(), io.StringIO()
    with (
      mock.patch.object(
        runner, "fetch_server_profile", side_effect=OSError("offline")
      ),
      contextlib.redirect_stdout(out),
      contextlib.redirect_stderr(err),
    ):
      code = runner.main(list(argv))
    return code, out.getvalue() + err.getvalue()

  def test_dry_run_writes_the_report(self):
    """Test that a dry run writes the report for the selected tests."""
    with tempfile.TemporaryDirectory() as directory:
      code, output = self.run_main(
        "--suite=shopping.checkout.totals_test",
        "--dry-run",
        f"--requirements_report={directory}",
      )
      data = json.loads((Path(directory) / report.JSON_FILE).read_text())
    self.assertEqual(code, 0, output)
    self.assertIn("Requirements report: ", output)
    self.assertEqual(len(data["tests"]), 5)
    self.assertEqual(data["markers"]["unmarked"], 0)

  def test_unwritable_directory_stops_the_run(self):
    """Test that a report that cannot be written fails the run."""
    with tempfile.NamedTemporaryFile() as not_a_directory:
      code, output = self.run_main(
        "--suite=shopping.checkout.totals_test",
        "--dry-run",
        f"--requirements_report={not_a_directory.name}",
      )
    self.assertEqual(code, 1)
    self.assertIn("Requirements report failed:", output)


if __name__ == "__main__":
  unittest.main()

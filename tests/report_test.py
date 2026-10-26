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
import dataclasses
from html.parser import HTMLParser
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
      self.assertEqual(
        paths,
        [
          out / report.JSON_FILE,
          out / report.MARKDOWN_FILE,
          out / report.HTML_FILE,
        ],
      )
      first = [path.read_bytes() for path in paths]
      self.assertEqual(json.loads(first[0]), report.to_json(self.report))
      self.assertEqual(first[1].decode(), report.to_markdown(self.report))
      self.assertEqual(first[2].decode(), report.to_html(self.report))
      report.write(self.report, out)
      self.assertEqual([path.read_bytes() for path in paths], first)

  def test_write_links_the_findings(self):
    """Test that write links the findings document relative to the output."""
    with tempfile.TemporaryDirectory() as directory:
      findings = Path(directory) / "findings.md"
      paths = report.write(self.report, Path(directory) / "out", findings)
      markdown = paths[1].read_text()
    self.assertIn("- **Findings:** [findings.md](../findings.md)", markdown)

  def test_summary(self):
    """Test the console lines."""
    text = report.summary(self.report, [Path("out/matrix.json")])
    self.assertIn("Requirements report: out (matrix.json)", text)
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


class TagBalance(HTMLParser):
  """Track open elements, to check that a page closes what it opens."""

  VOID = frozenset({"br", "hr", "img", "input", "link", "meta"})

  def __init__(self):
    """Start with no open elements."""
    super().__init__()
    self.open: list[str] = []
    self.mismatched: list[str] = []

  def handle_starttag(self, tag, attrs):
    """Open an element, unless it has no end tag."""
    del attrs
    if tag not in self.VOID:
      self.open.append(tag)

  def handle_endtag(self, tag):
    """Close the innermost element, noting a mismatch."""
    if not self.open or self.open.pop() != tag:
      self.mismatched.append(tag)


TRICKY = Catalog(
  rules=MappingProxyType(
    {
      "REQ-SHOPPING-CART-0009": dataclasses.replace(
        rule("REQ-SHOPPING-CART-0009", (CART,)),
        clause="Use `a|b` or c | d, per [RFC 9535](https://example.com/rfc)"
        '{ target="_blank" }, never <b>bold</b>; **MUST** apply.',
      )
    }
  ),
  retired=frozenset(),
)


class RenderTest(unittest.TestCase):
  """Tests for to_markdown, to_html and rule_groups."""

  def setUp(self):
    """Build the report for the small catalog."""
    super().setUp()
    self.report = report.build(CATALOG, TESTS)

  def test_markdown_summary(self):
    """Test the headline and the summary table, extensions last."""
    text = report.to_markdown(self.report)
    self.assertTrue(text.startswith("# Shopping requirements coverage\n"))
    self.assertIn(
      "**2 of the 4 testable MUST and MUST NOT rules have a test (50%), and "
      "3 of the 6 testable rules.** 6 shopping tests: 4 cite rules, "
      "1 `no_rule`, 0 `scenario`, 0 `outside_catalog`, 0 `older_version`, "
      "1 unmarked.",
      text,
    )
    rows = [
      "| cart | – | 1 | 1 | 0 | 1 | 0 | 1 | 0% |",
      "| checkout | – | 4 | 3 | 2 | 2 | 1 | 1 | 50% |",
      "| catalog.lookup | – | 1 | 1 | 1 | 1 | 1 | 0 | 100% |",
      "| catalog.search | – | 1 | 1 | 1 | 1 | 1 | 0 | 100% |",
      "| other | – | 1 | 1 | 0 | 0 | 0 | 0 | – |",
      "| discount | cart, checkout | 0 | 0 | 0 | 0 | 0 | 0 | – |",
      "| **All rules, each counted once** |  | 7 | 6 | 3 | 4 | 2 | 2 | 50% |",
    ]
    positions = [text.index(row) for row in rows]
    self.assertEqual(positions, sorted(positions))

  def test_markdown_sections(self):
    """Test a capability's section, the tests and the notes."""
    text = report.to_markdown(self.report)
    source = (
      "[checkout/index.md:1](https://github.com/Universal-Commerce-Protocol/"
      "ucp/blob/abc123def456/docs/specification/shopping/checkout/index.md"
      "?plain=1#L1)"
    )
    for expected in (
      "## checkout\n\n`dev.ucp.shopping.checkout`, a capability: 4 rules, 3 "
      "of them testable. 1 of its 2 testable MUST and MUST NOT rules have a "
      "test (50%).",
      "### Rules with a test: 2",
      "| CHECKOUT-0001 | MUST | The business MUST do "
      "REQ-SHOPPING-CHECKOUT-0001. | `ExampleTest.test_a`<br>"
      f"`ExampleTest.test_b` | {source} |",
      "### Testable MUST and MUST NOT rules without a test: 1",
      "<summary>Other testable rules without a test: 0</summary>\n\nNone.",
      "<summary>Not testable by this suite: 1</summary>",
      "| CHECKOUT-0004 | MUST | The business MUST do "
      f"REQ-SHOPPING-CHECKOUT-0004. | MCP page | {source} |",
      "`dev.ucp.shopping.discount`, an extension of cart, checkout: 0 rules",
      "### Tests that cite rules (`spec_assert`): 4",
      "| `ExampleTest.test_d` | `shopping/checkout/example_test.py` | "
      "CHECKOUT-0005, CHECKOUT-0099 |",
      "| `ExampleTest.test_e` | `shopping/checkout/example_test.py` | "
      "no keyword |",
      "### Checks of one store's data or features (`scenario`): 0\n\nNone.",
      "### Tests without a marker: 1",
      "- **IDs:** Rule IDs drop the `REQ-SHOPPING-` prefix.",
      "and the sentence the review found isn't a rule is left out.",
      "- **Shared rules:** 1 rule belongs to more than one capability.",
    ):
      self.assertIn(expected, text)
    self.assertNotIn("**Findings:**", text)
    self.assertIn(
      "- **Findings:** [findings.md](../findings.md) reviews each claim",
      report.to_markdown(self.report, "../findings.md"),
    )

  def test_html(self):
    """Test the page: headline cards, anchors, and only its own resources."""
    page = report.to_html(self.report, "../findings.md")
    self.assertTrue(page.startswith("<!DOCTYPE html>\n"))
    for expected in (
      '<div class="big">2 / 4</div>',
      '<a href="#cap-catalog-lookup">catalog.lookup</a>',
      '<section id="cap-catalog-lookup">',
      '<span style="width: 50%"></span>',
      '<tr class="total"><td>All rules, each counted once</td>',
      "<details open><summary>Rules with a test (2)</summary>",
      "<details><summary>Not testable by this suite (1)</summary>",
      "<code>ExampleTest.test_a</code><br><code>ExampleTest.test_b</code>",
      '<a href="../findings.md">findings.md</a>',
    ):
      self.assertIn(expected, page)
    for external in ("<script", "<link", " src="):
      self.assertNotIn(external, page)
    balance = TagBalance()
    balance.feed(page)
    self.assertEqual((balance.open, balance.mismatched), ([], []))

  def test_rule_text_is_escaped(self):
    """Test pipes, links and markup in rule text, in both outputs."""
    built = report.build(TRICKY, ())
    markdown = report.to_markdown(built)
    page = report.to_html(built)
    self.assertIn(
      "| CART-0009 | MUST | Use `a\\|b` or c \\| d, per RFC 9535, never "
      "&lt;b>bold&lt;/b>; **MUST** apply. | checkout/index.md:1 |",
      markdown,
    )
    self.assertIn(
      "<td>Use <code>a|b</code> or c | d, per RFC 9535, never "
      "&lt;b&gt;bold&lt;/b&gt;; <strong>MUST</strong> apply.</td>",
      page,
    )
    for text in (markdown, page):
      self.assertNotIn("example.com", text)
      self.assertNotIn("_blank", text)

  def test_rule_groups_match_the_counts(self):
    """Test that each capability's groups add up to its counts."""
    for row in self.report.capabilities:
      groups = report.rule_groups(self.report, row.capability.name)
      tally = row.tally
      with self.subTest(row.capability.name):
        self.assertEqual(len(groups.covered), tally.covered)
        self.assertEqual(len(groups.uncovered_must), tally.uncovered_must)
        self.assertEqual(
          len(groups.covered) + len(groups.uncovered_must) + len(groups.other),
          tally.testable,
        )
        self.assertEqual(len(groups.untestable), tally.rules - tally.testable)
    checkout = report.rule_groups(self.report, CHECKOUT)
    self.assertEqual(
      [rule.id for rule in checkout.untestable], ["REQ-SHOPPING-CHECKOUT-0004"]
    )


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
    """Test that generate writes the three files and describes them."""
    with tempfile.TemporaryDirectory() as directory:
      text = report.generate((), directory)
      out = Path(directory)
      data = json.loads((out / report.JSON_FILE).read_text())
      markdown = (out / report.MARKDOWN_FILE).read_text()
      page = (out / report.HTML_FILE).read_text()
    self.assertIn("507 rules, 217 testable", text)
    self.assertIn("(matrix.json, matrix.md, matrix.html)", text)
    self.assertEqual(data["tests"], [])
    self.assertIn("- **Findings:** [findings.md](", markdown)
    self.assertIn('<div class="big">0 / 104</div>', page)


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
      out = Path(directory)
      data = json.loads((out / report.JSON_FILE).read_text())
      written = sorted(path.name for path in out.iterdir())
    self.assertEqual(code, 0, output)
    self.assertIn("Requirements report: ", output)
    self.assertEqual(written, ["matrix.html", "matrix.json", "matrix.md"])
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

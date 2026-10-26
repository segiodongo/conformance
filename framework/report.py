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

The report is written three ways: `matrix.json` for tools, `matrix.md` to
read on GitHub, and `matrix.html`, one page with no external resources.

A rule counts as covered when a test's `spec_assert` marker cites it: the
report says which rules have a test, not whether the tests pass.
"""

import collections
from collections.abc import Callable, Iterable, Mapping, Sequence
import dataclasses
import html
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from framework.decorators import MARKER_KINDS
from framework.marker_check import LoadedTest, in_scope
from framework.requirements import (
  DEFAULT_CATALOG_DIR,
  Capability,
  Catalog,
  Rule,
  load_catalog,
)

FORMAT_VERSION = 1
JSON_FILE = "matrix.json"
MARKDOWN_FILE = "matrix.md"
HTML_FILE = "matrix.html"
# The findings document, next to the catalog directory.
FINDINGS_FILE = "findings.md"
TITLE = "Shopping requirements coverage"
# The levels of an absolute requirement or prohibition.
MUST_LEVELS = frozenset({"MUST", "MUST NOT", "SHALL", "SHALL NOT"})
UNMARKED = "unmarked"
ID_PREFIX = "REQ-SHOPPING-"
CAPABILITY_PREFIX = "dev.ucp.shopping."
SPEC_PAGES = "docs/specification/shopping/"
SPEC_REPO = "https://github.com/Universal-Commerce-Protocol/ucp"
KIND_TITLES = {
  "spec_assert": "Tests that cite rules",
  "no_rule": "Behaviour the spec states without a keyword",
  "scenario": "Checks of one store's data or features",
  "outside_catalog": "Rules on pages outside the catalog",
  "older_version": "Checks of an older release",
  UNMARKED: "Tests without a marker",
}
# A Markdown link, with the attribute list that may follow it, as in
# `[RFC 9535](https://www.rfc-editor.org/rfc/rfc9535){ target="_blank" }`.
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)(?:\{[^}]*\})?")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_CSS = """
body { font: 14px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial,
  sans-serif; color: #1f2328; max-width: 1200px; margin: 0 auto;
  padding: 24px; }
h1 { font-size: 26px; margin: 0 0 8px; }
h2 { border-bottom: 1px solid #d0d7de; padding-bottom: 4px; margin-top: 36px; }
h3 { margin-top: 28px; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace;
  background: #f6f8fa; padding: 1px 4px; border-radius: 4px; }
a { color: #0969da; }
.note { color: #59636e; }
nav a { margin-right: 16px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 20px 0; }
.card { flex: 1; min-width: 200px; border: 1px solid #d0d7de;
  border-radius: 8px; padding: 12px 16px; }
.card .big { font-size: 26px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; }
th, td { border: 1px solid #d0d7de; padding: 4px 8px; text-align: left;
  vertical-align: top; }
th { background: #f6f8fa; }
td.num { text-align: right; white-space: nowrap; }
td.id, td.level, td.cov { white-space: nowrap; }
tr.total td { font-weight: 600; background: #f6f8fa; }
.bar { display: inline-block; width: 90px; height: 10px; border-radius: 5px;
  background: #eaeef2; vertical-align: middle; overflow: hidden; }
.bar span { display: block; height: 100%; background: #1a7f37; }
details { margin: 10px 0; }
summary { cursor: pointer; font-weight: 600; }
"""


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


@dataclasses.dataclass(frozen=True)
class RuleGroups:
  """A capability's rules, grouped as the report shows them.

  Attributes:
    covered: The testable rules at least one test cites.
    uncovered_must: The testable MUST and MUST NOT rules no test cites.
    other: The other testable rules no test cites.
    untestable: The rules this suite cannot test.

  """

  covered: tuple[Rule, ...]
  uncovered_must: tuple[Rule, ...]
  other: tuple[Rule, ...]
  untestable: tuple[Rule, ...]


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


def rule_groups(report: Report, capability: str) -> RuleGroups:
  """Group one capability's rules for the report's drill-down.

  Args:
    report: The report.
    capability: The capability's name.

  Returns:
    Its rules, grouped.

  """
  rules = [rule for rule in report.rules if capability in rule.capabilities]
  testable = [rule for rule in rules if rule.testable]
  uncited = [rule for rule in testable if rule.id not in report.cited_by]
  return RuleGroups(
    covered=tuple(rule for rule in testable if rule.id in report.cited_by),
    uncovered_must=tuple(r for r in uncited if r.level in MUST_LEVELS),
    other=tuple(r for r in uncited if r.level not in MUST_LEVELS),
    untestable=tuple(rule for rule in rules if not rule.testable),
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


def to_markdown(report: Report, findings: str | None = None) -> str:
  """Return the report as Markdown that renders on GitHub.

  Args:
    report: The report.
    findings: A link to the findings document, if there is one.

  Returns:
    The Markdown.

  """
  lines = [
    f"# {TITLE}",
    "",
    f"> **Demo only.** {_intro(report, _md_code)}",
    "",
    f"**{_headline(report)}** {_marker_counts(report, _md_code)}",
    "",
    "## Summary",
    "",
    "| Capability | Extends | Rules | Testable | With a test | Testable MUST "
    "| MUST with a test | MUST without a test | MUST coverage |",
    "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
  ]
  for row in _ordered(report):
    extends = ", ".join(_short_cap(name) for name in row.capability.extends)
    lines.append(
      _md_tally_row(_short_cap(row.capability.name), extends or "–", row.tally)
    )
  lines.append(
    _md_tally_row("**All rules, each counted once**", "", report.total)
  )
  for row in _ordered(report):
    lines += ["", *_md_capability(report, row)]
  lines += ["", *_md_tests(report), "", "## Notes", ""]
  lines += [
    f"- **{label}:** {text}"
    for label, text in _notes(report, _md_code, _md_link, findings)
  ]
  return "\n".join(lines) + "\n"


def to_html(report: Report, findings: str | None = None) -> str:
  """Return the report as one HTML page with no external resources.

  Args:
    report: The report.
    findings: A link to the findings document, if there is one.

  Returns:
    The HTML.

  """
  total = report.total
  head = [
    "<!DOCTYPE html>",
    '<html lang="en">',
    "<head>",
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    f"<title>{TITLE}</title>",
    f"<style>{_CSS}</style>",
    "</head>",
    "<body>",
    f"<h1>{TITLE}</h1>",
    f'<p class="note"><strong>Demo only.</strong> '
    f"{_intro(report, _html_code)}</p>",
    '<div class="cards">',
    _card(
      f"{total.covered_must} / {total.testable_must}",
      "testable MUST and MUST NOT rules have a test "
      f"({_percent(total.must_coverage)})",
    ),
    _card(f"{total.covered} / {total.testable}", "testable rules have a test"),
    _card(str(total.rules), "rules in the catalog"),
    _card(
      str(len(report.tests)),
      "shopping tests: " + ", ".join(_marker_parts(report, _html_code)),
    ),
    "</div>",
    '<nav><a href="#summary">Summary</a><a href="#capabilities">'
    'Capabilities</a><a href="#tests">Tests</a><a href="#notes">Notes</a>'
    "</nav>",
    '<h2 id="summary">Summary</h2>',
    _html_summary(report),
    '<h2 id="capabilities">Capabilities</h2>',
  ]
  body = [_html_capability(report, row) for row in _ordered(report)]
  notes = "".join(
    f"<li><strong>{label}:</strong> {text}</li>"
    for label, text in _notes(report, _html_code, _html_link, findings)
  )
  tail = [
    _html_tests(report),
    '<h2 id="notes">Notes</h2>',
    f"<ul>{notes}</ul>",
    "</body>",
    "</html>",
  ]
  return "\n".join([*head, *body, *tail]) + "\n"


def write(
  report: Report,
  directory: str | Path,
  findings: str | Path | None = None,
) -> list[Path]:
  """Write the report's files to a directory, creating it if need be.

  Args:
    report: The report.
    directory: Where to write it.
    findings: The findings document to link to, if there is one.

  Returns:
    The paths written: `matrix.json`, `matrix.md` and `matrix.html`.

  """
  out = Path(directory)
  out.mkdir(parents=True, exist_ok=True)
  href = None
  if findings:
    relative = os.path.relpath(Path(findings).resolve(), out.resolve())
    href = Path(relative).as_posix()
  texts = {
    JSON_FILE: json.dumps(to_json(report), indent=2, ensure_ascii=False) + "\n",
    MARKDOWN_FILE: to_markdown(report, href),
    HTML_FILE: to_html(report, href),
  }
  paths = []
  for name, text in texts.items():
    path = out / name
    path.write_text(text, encoding="utf-8")
    paths.append(path)
  return paths


def summary(report: Report, paths: Sequence[Path]) -> str:
  """Return a few lines for the console about a written report."""
  total = report.total
  share = total.must_coverage
  commit = report.spec_commit[:8] or "an unknown commit"
  if len({path.parent for path in paths}) == 1:
    names = ", ".join(path.name for path in paths)
    where = f"{paths[0].parent} ({names})"
  else:
    where = ", ".join(str(path) for path in paths)
  return (
    f"Requirements report: {where}\n"
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
    catalog_dir: The catalog directory; by default this repository's. The
      findings document beside it, if any, is linked from the report.

  Returns:
    A few lines for the console.

  Raises:
    CatalogError: If the catalog cannot be loaded.
    OSError: If the report cannot be written.

  """
  catalog_root = Path(catalog_dir or DEFAULT_CATALOG_DIR).resolve()
  report = build(load_catalog(catalog_root), tests)
  findings = catalog_root.parent / FINDINGS_FILE
  paths = write(report, directory, findings if findings.is_file() else None)
  return summary(report, paths)


# Building.


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


# Wording shared by the Markdown and the HTML. `code` and `link` format
# code and links for the output; the rest of the wording needs no escaping.


def _intro(report: Report, code: Callable[[str], str]) -> str:
  """Return the sentence that says what the report is."""
  commit = report.spec_commit[:8] or "an unknown commit"
  return (
    f"Built from the requirements catalog at {code(commit)} and the "
    f"shopping tests' markers. A rule counts as covered when a test's "
    f"{code('spec_assert')} marker cites it: the report says which rules "
    "have a test, not whether the tests pass."
  )


def _headline(report: Report) -> str:
  """Return the sentence with the coverage numbers."""
  total = report.total
  return (
    f"{total.covered_must} of the {total.testable_must} testable MUST and "
    f"MUST NOT rules have a test ({_percent(total.must_coverage)}), and "
    f"{total.covered} of the {total.testable} testable rules."
  )


def _marker_parts(report: Report, code: Callable[[str], str]) -> list[str]:
  """Return how many tests carry each kind of marker, as phrases."""
  markers = report.markers()
  return [
    f"{markers['spec_assert']} cite rules",
    *(f"{markers[kind]} {code(kind)}" for kind in MARKER_KINDS[1:]),
    f"{markers[UNMARKED]} unmarked",
  ]


def _marker_counts(report: Report, code: Callable[[str], str]) -> str:
  """Return the sentence with the marker counts."""
  parts = ", ".join(_marker_parts(report, code))
  return f"{len(report.tests)} shopping tests: {parts}."


def _capability_sentence(row: CapabilityRow, code: Callable[[str], str]) -> str:
  """Return the sentence that introduces a capability's section."""
  capability, tally = row.capability, row.tally
  if capability.kind == "extension" and capability.extends:
    parents = ", ".join(_short_cap(name) for name in capability.extends)
    kind = f"an extension of {parents}"
  elif capability.kind == "extension":
    kind = "an extension"
  else:
    kind = "a capability"
  return (
    f"{code(capability.name)}, {kind}: {tally.rules} rules, "
    f"{tally.testable} of them testable. {tally.covered_must} of its "
    f"{tally.testable_must} testable MUST and MUST NOT rules have a test "
    f"({_percent(tally.must_coverage)})."
  )


def _notes(
  report: Report,
  code: Callable[[str], str],
  link: Callable[[str, str], str],
  findings: str | None,
) -> list[tuple[str, str]]:
  """Return the notes at the end of the report, as labels and text."""
  commit = report.spec_commit[:8] or "an unknown commit"
  notes = []
  if findings:
    notes.append(
      (
        "Findings",
        f"{link(FINDINGS_FILE, findings)} reviews each claim and lists the "
        "spec and test findings from marking the tests.",
      )
    )
  if report.not_rules == 1:
    left_out = ", and the sentence the review found isn't a rule is left out"
  elif report.not_rules:
    left_out = (
      f", and the {report.not_rules} sentences the review found aren't rules "
      "are left out"
    )
  else:
    left_out = ""
  notes += [
    (
      "IDs",
      f"Rule IDs drop the {code(ID_PREFIX)} prefix. They are stable within "
      "this demo, not official.",
    ),
    (
      "Catalog",
      f"Rules stated in the specification pages under {code(SPEC_PAGES)} at "
      f"{code(commit)}. Keyword sentences in schema field descriptions "
      f"aren't included{left_out}.",
    ),
    (
      "Testable",
      "This suite plays the platform and calls a business over REST, so a "
      "rule is testable when it is on a transport-neutral or REST page and "
      "aimed at the business, as corrected by the review.",
    ),
    ("MUST", "Counts MUST and MUST NOT."),
  ]
  shared = sum(len(rule.capabilities) > 1 for rule in report.rules)
  if shared:
    rules = "1 rule belongs" if shared == 1 else f"{shared} rules belong"
    notes.append(
      (
        "Shared rules",
        f"{rules} to more than one capability. Each capability counts "
        "them; the total counts each rule once.",
      )
    )
  return notes


def _ordered(report: Report) -> list[CapabilityRow]:
  """Return the capabilities, then the extensions, in the index's order."""
  rows = list(report.capabilities)
  return [row for row in rows if row.capability.kind != "extension"] + [
    row for row in rows if row.capability.kind == "extension"
  ]


def _tests_by_kind(report: Report) -> list[tuple[str, list[TestRow]]]:
  """Return the tests grouped by marker kind, in the kinds' order."""
  return [
    (kind, [test for test in report.tests if test.kind == kind])
    for kind in (*MARKER_KINDS, UNMARKED)
  ]


def _test_header(kind: str) -> list[str]:
  """Return the column titles for tests of one kind."""
  if kind == "spec_assert":
    return ["Test", "File", "Rules"]
  if kind == "older_version":
    return ["Test", "File", "Release", "What changed"]
  if kind == UNMARKED:
    return ["Test", "File"]
  return ["Test", "File", "Reason"]


def _test_values(test: TestRow) -> list[str]:
  """Return what the report shows for a test, in its columns' order."""
  values = [_short_test(test.test_id), test.module.replace(".", "/") + ".py"]
  if test.kind == "spec_assert":
    values.append(", ".join(_short_id(rule_id) for rule_id in test.rule_ids))
  elif test.kind == "older_version":
    values += [test.version, test.reason]
  elif test.kind != UNMARKED:
    values.append(test.reason)
  return values


def _rule_header(extra: str) -> list[str]:
  """Return the column titles for a table of rules."""
  title = {"tests": "Tests", "why": "Why not testable"}.get(extra)
  return ["Rule", "Level", "Text", *([title] if title else []), "Source"]


def _short_id(rule_id: str) -> str:
  """Return a rule ID without its common prefix."""
  return rule_id.removeprefix(ID_PREFIX)


def _short_cap(name: str) -> str:
  """Return a capability's name without its common prefix."""
  return name.removeprefix(CAPABILITY_PREFIX)


def _short_test(test_id: str) -> str:
  """Return a test's class and method from its ID."""
  return ".".join(test_id.rsplit(".", 2)[-2:])


def _percent(share: float | None) -> str:
  """Return a share as a percentage, or a dash when there is none."""
  return "–" if share is None else f"{share:.0%}"


def _source_label(rule: Rule) -> str:
  """Return the page and line a rule is on."""
  return f"{rule.source_file.removeprefix(SPEC_PAGES)}:{rule.line}"


def _source_url(report: Report, rule: Rule) -> str:
  """Return a link to a rule's line at the catalog's commit, if known."""
  if not report.spec_commit:
    return ""
  return (
    f"{SPEC_REPO}/blob/{report.spec_commit}/{rule.source_file}"
    f"?plain=1#L{rule.line}"
  )


def _plain(text: str) -> str:
  """Return text on one line, with Markdown links reduced to their text."""
  return " ".join(_LINK.sub(r"\1", text).split())


# Markdown.


def _md_code(text: str) -> str:
  """Return text as Markdown code."""
  return f"`{text}`"


def _md_link(text: str, href: str) -> str:
  """Return a Markdown link."""
  return f"[{text}]({href})"


def _md_cell(text: str) -> str:
  """Return text for a Markdown table cell."""
  parts = _plain(text).split("`")
  parts[::2] = [part.replace("<", "&lt;") for part in parts[::2]]
  return "`".join(parts).replace("|", "\\|")


def _md_table(
  header: Sequence[str], rows: Iterable[Sequence[str]]
) -> list[str]:
  """Return a Markdown table."""
  lines = [
    "| " + " | ".join(header) + " |",
    "|" + " --- |" * len(header),
  ]
  lines += ["| " + " | ".join(row) + " |" for row in rows]
  return lines


def _md_tally_row(label: str, extends: str, tally: Tally) -> str:
  """Return a summary row."""
  cells = [
    label,
    extends,
    tally.rules,
    tally.testable,
    tally.covered,
    tally.testable_must,
    tally.covered_must,
    tally.uncovered_must,
    _percent(tally.must_coverage),
  ]
  return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _md_capability(report: Report, row: CapabilityRow) -> list[str]:
  """Return a capability's section."""
  groups = rule_groups(report, row.capability.name)
  return [
    f"## {_short_cap(row.capability.name)}",
    "",
    _capability_sentence(row, _md_code),
    "",
    f"### Rules with a test: {len(groups.covered)}",
    "",
    *_md_rules(report, groups.covered, "tests"),
    "",
    "### Testable MUST and MUST NOT rules without a test: "
    f"{len(groups.uncovered_must)}",
    "",
    *_md_rules(report, groups.uncovered_must, ""),
    "",
    "<details>",
    "<summary>Other testable rules without a test: "
    f"{len(groups.other)}</summary>",
    "",
    *_md_rules(report, groups.other, ""),
    "",
    "</details>",
    "",
    "<details>",
    f"<summary>Not testable by this suite: {len(groups.untestable)}</summary>",
    "",
    *_md_rules(report, groups.untestable, "why"),
    "",
    "</details>",
  ]


def _md_rules(report: Report, rules: Sequence[Rule], extra: str) -> list[str]:
  """Return a table of rules, or "None."; `extra` adds a column."""
  if not rules:
    return ["None."]
  rows = []
  for rule in rules:
    cells = [_short_id(rule.id), rule.level, _md_cell(rule.clause)]
    if extra == "tests":
      tests = report.cited_by.get(rule.id, ())
      cells.append("<br>".join(f"`{_short_test(t)}`" for t in tests))
    elif extra == "why":
      cells.append(_md_cell(rule.testable_reason))
    url = _source_url(report, rule)
    label = _source_label(rule)
    cells.append(f"[{label}]({url})" if url else label)
    rows.append(cells)
  return _md_table(_rule_header(extra), rows)


def _md_tests(report: Report) -> list[str]:
  """Return the tests section."""
  lines = ["## Tests"]
  for kind, tests in _tests_by_kind(report):
    if kind == UNMARKED and not tests:
      continue
    marker = "" if kind == UNMARKED else f" (`{kind}`)"
    lines += ["", f"### {KIND_TITLES[kind]}{marker}: {len(tests)}", ""]
    if not tests:
      lines.append("None.")
      continue
    rows = []
    for test in tests:
      values = _test_values(test)
      rows.append(
        [f"`{values[0]}`", f"`{values[1]}`", *map(_md_cell, values[2:])]
      )
    lines += _md_table(_test_header(kind), rows)
  return lines


# HTML.


def _html_code(text: str) -> str:
  """Return text as HTML code."""
  return f"<code>{html.escape(text)}</code>"


def _html_link(text: str, href: str) -> str:
  """Return an HTML link."""
  return f'<a href="{html.escape(href)}">{html.escape(text)}</a>'


def _html_text(text: str) -> str:
  """Return text as HTML, keeping its code spans and bold."""
  escaped = html.escape(_plain(text), quote=False)
  return _BOLD.sub(
    r"<strong>\1</strong>", _CODE.sub(r"<code>\1</code>", escaped)
  )


def _html_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
  """Return an HTML table; the cells are HTML already."""
  lines = [
    "<table>",
    "<tr>" + "".join(f"<th>{h}</th>" for h in header) + "</tr>",
  ]
  lines += ["<tr>" + "".join(row) + "</tr>" for row in rows]
  lines.append("</table>")
  return "\n".join(lines)


def _card(big: str, label: str) -> str:
  """Return a headline number with its label."""
  return f'<div class="card"><div class="big">{big}</div>{label}</div>'


def _anchor(name: str) -> str:
  """Return the HTML ID of a capability's section."""
  return "cap-" + _short_cap(name).replace(".", "-")


def _html_summary(report: Report) -> str:
  """Return the summary table."""
  header = [
    "Capability",
    "Extends",
    "Rules",
    "Testable",
    "With a test",
    "Testable MUST",
    "MUST with a test",
    "MUST without a test",
    "MUST coverage",
  ]
  rows = []
  for row in _ordered(report):
    name = row.capability.name
    label = _html_link(_short_cap(name), f"#{_anchor(name)}")
    extends = ", ".join(_short_cap(parent) for parent in row.capability.extends)
    rows.append(
      _html_tally_cells(label, html.escape(extends or "–"), row.tally)
    )
  total = _html_tally_cells("All rules, each counted once", "", report.total)
  table = _html_table(header, rows)
  return table.replace("</table>", f'<tr class="total">{total}</tr>\n</table>')


def _html_tally_cells(label: str, extends: str, tally: Tally) -> str:
  """Return a summary row's cells."""
  share = tally.must_coverage
  width = 0 if share is None else round(100 * share)
  numbers = "".join(
    f'<td class="num">{value}</td>'
    for value in (
      tally.rules,
      tally.testable,
      tally.covered,
      tally.testable_must,
      tally.covered_must,
      tally.uncovered_must,
    )
  )
  bar = f'<span class="bar"><span style="width: {width}%"></span></span>'
  return (
    f"<td>{label}</td><td>{extends}</td>{numbers}"
    f'<td class="cov">{bar} {_percent(share)}</td>'
  )


def _html_capability(report: Report, row: CapabilityRow) -> str:
  """Return a capability's section."""
  groups = rule_groups(report, row.capability.name)
  sections = [
    ("Rules with a test", groups.covered, "tests", True),
    (
      "Testable MUST and MUST NOT rules without a test",
      groups.uncovered_must,
      "",
      True,
    ),
    ("Other testable rules without a test", groups.other, "", False),
    ("Not testable by this suite", groups.untestable, "why", False),
  ]
  parts = [
    f'<section id="{_anchor(row.capability.name)}">',
    f"<h3>{html.escape(_short_cap(row.capability.name))}</h3>",
    f"<p>{_capability_sentence(row, _html_code)}</p>",
  ]
  for title, rules, extra, expanded in sections:
    is_open = " open" if expanded else ""
    parts += [
      f"<details{is_open}><summary>{title} ({len(rules)})</summary>",
      _html_rules(report, rules, extra),
      "</details>",
    ]
  parts.append("</section>")
  return "\n".join(parts)


def _html_rules(report: Report, rules: Sequence[Rule], extra: str) -> str:
  """Return a table of rules, or "None."; `extra` adds a column."""
  if not rules:
    return "<p>None.</p>"
  rows = []
  for rule in rules:
    cells = [
      f'<td class="id">{html.escape(_short_id(rule.id))}</td>',
      f'<td class="level">{html.escape(rule.level)}</td>',
      f"<td>{_html_text(rule.clause)}</td>",
    ]
    if extra == "tests":
      tests = report.cited_by.get(rule.id, ())
      names = "<br>".join(_html_code(_short_test(t)) for t in tests)
      cells.append(f"<td>{names}</td>")
    elif extra == "why":
      cells.append(f"<td>{_html_text(rule.testable_reason)}</td>")
    url = _source_url(report, rule)
    label = _source_label(rule)
    source = _html_link(label, url) if url else html.escape(label)
    cells.append(f"<td>{source}</td>")
    rows.append(cells)
  return _html_table(_rule_header(extra), rows)


def _html_tests(report: Report) -> str:
  """Return the tests section."""
  parts = ['<h2 id="tests">Tests</h2>']
  for kind, tests in _tests_by_kind(report):
    if kind == UNMARKED and not tests:
      continue
    marker = "" if kind == UNMARKED else f" ({_html_code(kind)})"
    parts.append(f"<h3>{KIND_TITLES[kind]}{marker}: {len(tests)}</h3>")
    if not tests:
      parts.append("<p>None.</p>")
      continue
    rows = []
    for test in tests:
      values = _test_values(test)
      cells = [_html_code(values[0]), _html_code(values[1])]
      cells += [_html_text(value) for value in values[2:]]
      rows.append([f"<td>{cell}</td>" for cell in cells])
    parts.append(_html_table(_test_header(kind), rows))
  return "\n".join(parts)

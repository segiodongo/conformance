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

# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

"""Suggest catalog rules for each shopping test.

Demo only. For every test under `shopping/`, ranks the testable rules of the
capabilities the test requires by the words they share with it: its name,
its docstring, and the strings and names in its body and in the helper
methods of its class that it calls. Writes a worksheet with the best
candidates for each test, and marker lines ready to paste.

These are guesses. Read each test's assertions before marking it.

The tests are read with `ast`, not imported, so this needs no dependencies.

Usage, from the repository root:

  uv run --no-project requirements/tools/suggest_rules.py
"""

import argparse
import ast
import collections
from collections.abc import Iterator
import dataclasses
import importlib.util
import math
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "shopping"
DEFAULT_OUT = REPO_ROOT / "requirements" / "reports" / "suggestions.md"
TOP = 5
# The summary lists this many tests whose best candidate scores lowest. A
# low score can't tell a no_rule or scenario test apart: those still share
# words with the rules of their capability.
LEAST_CERTAIN = 8
# Marker lines are indented this far in a test file and fit in 80 columns.
MARKER_INDENT = "    "
LINE_LENGTH = 80
# How much a word counts, by where in the test it appears.
WEIGHTS = {
  "name": 3.0,
  "docstring": 2.0,
  "string": 2.0,
  "identifier": 1.0,
  "helper": 1.0,
  "module": 0.5,
}
STOPWORDS_BY_KIND = (
  # English
  "a an and are as at be been but by can do does each for from has have if",
  "in into is it its no not of on or so than that the their them then there",
  "these this those to was were when where which while will with",
  # RFC 2119 keywords, which every rule has
  "must should may shall required optional recommended",
  # Python and unittest
  "self none true false def return len str int dict list cls kwargs args",
  "test tests given json response subtest fail assertraises",
  "assertequal assertnotequal assertin assertnotin asserttrue assertfalse",
  "assertisnone assertisnotnone assertisinstance assertgreater",
  "assertgreaterequal assertless assertlessequal",
)
STOPWORDS = frozenset(
  word for group in STOPWORDS_BY_KIND for word in group.split()
)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def stem(word: str) -> str:
  """Reduce a plural to its singular, roughly: entries, codes, totals."""
  if len(word) > 4 and word.endswith("ies"):
    return word[:-3] + "y"
  if (
    len(word) > 3
    and word.endswith("s")
    and not word.endswith(("ss", "us", "is"))
  ):
    return word[:-1]
  return word


def words(text: str) -> Iterator[str]:
  """Yield the words of a text, field names whole and in parts."""
  for match in WORD_RE.finditer(text):
    word = match.group(0).lower()
    parts = [part for part in word.split("_") if part]
    for candidate in ([word] if len(parts) > 1 else []) + parts:
      if len(candidate) > 1 and candidate not in STOPWORDS:
        yield stem(candidate)


@dataclasses.dataclass
class FoundTest:
  """One test found in the shopping suite.

  Attributes:
    file: The test's file, relative to the repository root.
    name: The test's class and method names.
    summary: The first line of its docstring.
    capabilities: The capabilities it requires.
    words: Its words, weighted by where they appear.

  """

  file: str
  name: str
  summary: str
  capabilities: frozenset[str]
  words: collections.Counter[str]


def module_constants(tree: ast.Module) -> dict[str, str]:
  """Return a module's top-level string constants, by name."""
  constants = {}
  for node in tree.body:
    if isinstance(node, ast.Assign):
      targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
      targets, value = [node.target], node.value
    else:
      continue
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
      for target in targets:
        if isinstance(target, ast.Name):
          constants[target.id] = value.value
  return constants


def required(decorators: list[ast.expr], constants: dict[str, str]) -> set[str]:
  """Return the capabilities named by `requires_capability` decorators."""
  names = set()
  for decorator in decorators:
    if not isinstance(decorator, ast.Call) or not decorator.args:
      continue
    func = decorator.func
    called = (
      func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    )
    if called != "requires_capability":
      continue
    argument = decorator.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
      names.add(argument.value)
    elif isinstance(argument, ast.Name) and argument.id in constants:
      names.add(constants[argument.id])
  return names


def body_words(
  function: ast.FunctionDef,
) -> tuple[collections.Counter[str], collections.Counter[str], set[str]]:
  """Return a function's string words, its name words, and the self calls.

  The docstring is left out; it is counted on its own.
  """
  strings: collections.Counter[str] = collections.Counter()
  names: collections.Counter[str] = collections.Counter()
  calls = set()
  docstring = None
  if function.body and isinstance(function.body[0], ast.Expr):
    docstring = function.body[0].value
  for node in ast.walk(function):
    if node is docstring:
      continue
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
      strings.update(words(node.value))
    elif isinstance(node, ast.Attribute):
      names.update(words(node.attr))
      if isinstance(node.value, ast.Name) and node.value.id == "self":
        calls.add(node.attr)
    elif isinstance(node, ast.Name):
      names.update(words(node.id))
  return strings, names, calls


def weighted(
  counts: collections.Counter[str], weight: float
) -> dict[str, float]:
  """Return word counts scaled by a weight."""
  return {word: count * weight for word, count in counts.items()}


def find_tests(tests_dir: Path) -> list[FoundTest]:
  """Find every test method in the shopping suite's test files."""
  found = []
  for path in sorted(tests_dir.rglob("*_test.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = module_constants(tree)
    module_words = collections.Counter(words(ast.get_docstring(tree) or ""))
    for test_class in (n for n in tree.body if isinstance(n, ast.ClassDef)):
      class_needs = required(test_class.decorator_list, constants)
      methods = {
        node.name: node
        for node in test_class.body
        if isinstance(node, ast.FunctionDef)
      }
      for method in methods.values():
        if not method.name.startswith("test_"):
          continue
        docstring = ast.get_docstring(method) or ""
        strings, names, calls = body_words(method)
        total: collections.Counter[str] = collections.Counter()
        parts = [
          weighted(collections.Counter(words(method.name)), WEIGHTS["name"]),
          weighted(collections.Counter(words(docstring)), WEIGHTS["docstring"]),
          weighted(strings, WEIGHTS["string"]),
          weighted(names, WEIGHTS["identifier"]),
          weighted(module_words, WEIGHTS["module"]),
        ]
        for helper in sorted(calls & set(methods) - {method.name}):
          helper_strings, helper_names, _ = body_words(methods[helper])
          parts.append(
            weighted(helper_strings + helper_names, WEIGHTS["helper"])
          )
        for part in parts:
          total.update(part)
        found.append(
          FoundTest(
            file=path.relative_to(REPO_ROOT).as_posix(),
            name=f"{test_class.name}.{method.name}",
            summary=docstring.splitlines()[0] if docstring else "",
            capabilities=frozenset(
              class_needs | required(method.decorator_list, constants)
            ),
            words=total,
          )
        )
  return found


def load_catalog_module() -> ModuleType:
  """Import `framework/requirements.py` on its own.

  Importing it through the `framework` package would run the package's
  `__init__`, which needs the test suite's dependencies.
  """
  path = REPO_ROOT / "framework" / "requirements.py"
  spec = importlib.util.spec_from_file_location("catalog_lookup", path)
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


def rule_words(rule: Any) -> collections.Counter[str]:
  """Return a rule's words: its wording and its section's anchor."""
  anchor = rule.link.partition("#")[2].replace("-", " ")
  return collections.Counter(words(rule.clause)) + collections.Counter(
    words(anchor)
  )


def plain(clause: str) -> str:
  """Return a clause without Markdown, on one line."""
  text = MARKDOWN_LINK_RE.sub(r"\1", clause)
  text = text.replace("**", "").replace("`", "")
  return " ".join(text.split())


def marker_line(rule_id: str, clause: str) -> str:
  """Return a `spec_assert` argument line, with the wording as a comment."""
  prefix = f'{MARKER_INDENT}"{rule_id}",  # '
  comment = plain(clause)
  room = LINE_LENGTH - len(prefix)
  if len(comment) > room:
    comment = comment[: room - 3].rsplit(" ", 1)[0] + "..."
  return prefix + comment


def rank(
  tests: list[FoundTest], catalog: Any
) -> list[tuple[FoundTest, list[tuple[float, Any]]]]:
  """Rank each test's candidate rules by TF-IDF cosine similarity.

  Word rarity is measured across the whole catalog, so words every rule
  shares count for little. A test's candidates are the testable rules of
  the capabilities it requires, or that those extend.

  Args:
    tests: The tests found.
    catalog: The requirements catalog.

  Returns:
    Each test with its best candidates, best first, as (score, rule).

  """
  rules = list(catalog.rules.values())
  vectors = {rule.id: rule_words(rule) for rule in rules}
  frequency = collections.Counter(
    word for counts in vectors.values() for word in counts
  )
  rarity = {
    word: math.log((len(rules) + 1) / (count + 1)) + 1
    for word, count in frequency.items()
  }

  def normalized(counts: dict[str, float]) -> dict[str, float]:
    vector = {w: c * rarity[w] for w, c in counts.items() if w in rarity}
    norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
    return {word: value / norm for word, value in vector.items()}

  rule_vectors = {rule_id: normalized(c) for rule_id, c in vectors.items()}
  ranked = []
  for test in tests:
    covered = catalog.with_parents(test.capabilities)
    query = normalized(test.words)
    scored = sorted(
      (
        (
          sum(
            weight * rule_vectors[rule.id].get(word, 0.0)
            for word, weight in query.items()
          ),
          rule,
        )
        for rule in rules
        if rule.is_rule
        and rule.testable
        and (not covered or covered.intersection(rule.capabilities))
      ),
      key=lambda pair: (-pair[0], pair[1].id),
    )
    ranked.append((test, scored[:TOP]))
  return ranked


def worksheet(ranked: list[tuple[FoundTest, list[tuple[float, Any]]]]) -> str:
  """Return the worksheet as Markdown."""
  files = sorted({test.file for test, _ in ranked})
  lines = [
    "# Rule suggestions for the shopping tests",
    "",
    "Generated by `requirements/tools/suggest_rules.py`. For each test, the",
    "testable rules of the capabilities it requires, ranked by the words they",
    "share with the test. These are guesses: read the test's assertions",
    "before marking it.",
    "",
    f"{len(ranked)} tests in {len(files)} files.",
  ]
  for file in files:
    lines += ["", f"## {file}"]
    for test, candidates in ranked:
      if test.file != file:
        continue
      capabilities = ", ".join(
        sorted(c.removeprefix("dev.ucp.shopping.") for c in test.capabilities)
      )
      lines += [
        "",
        f"### {test.name}",
        "",
        f"Requires: {capabilities or 'nothing declared'}. {test.summary}",
        "",
        "| Score | ID | Level | Rule |",
        "| ----: | -- | ----- | ---- |",
      ]
      for score, rule in candidates:
        wording = plain(rule.clause).replace("|", "\\|")
        if len(wording) > 160:
          wording = wording[:157].rsplit(" ", 1)[0] + "..."
        lines.append(
          f"| {score:.2f} | `{rule.id}` | {rule.level} | {wording} |"
        )
      lines += ["", "```python", "  @spec_assert("]
      lines += [marker_line(rule.id, rule.clause) for _, rule in candidates]
      lines += ["  )", "```"]
  return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
  """Write the worksheet and print a summary."""
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
  args = parser.parse_args(argv)
  out = sys.stdout.write

  catalog = load_catalog_module().load_catalog()
  tests = find_tests(TESTS_DIR)
  ranked = rank(tests, catalog)
  args.out.parent.mkdir(parents=True, exist_ok=True)
  args.out.write_text(worksheet(ranked), encoding="utf-8")

  by_file = collections.Counter(test.file for test in tests)
  out(f"{len(tests)} tests in {len(by_file)} files\n")
  best = sorted(
    (candidates[0][0] if candidates else 0.0, test.name)
    for test, candidates in ranked
  )
  out("least certain (lowest best score), to read with most care:\n")
  for score, name in best[:LEAST_CERTAIN]:
    out(f"  {score:.2f}  {name}\n")
  out(f"wrote {args.out}\n")
  return 0


if __name__ == "__main__":
  sys.exit(main())

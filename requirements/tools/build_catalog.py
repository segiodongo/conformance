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
# dependencies = [
#   "markdown-it-py==3.0.0",
#   "markdown==3.10.2",
#   "jsonschema==4.26.0",
# ]
# ///

"""Build the demo requirements catalog for the shopping vertical.

Demo only. This wraps the requirements extractor from the `.github`
repository (`org-tools/requirements-extractor`) and adds two options the
extractor does not have yet:

- a capability for every page, from its capability bullet or else its file
  path (a preview of the planned file-path rule); and
- counting upper-case RFC 2119 keywords that are not bold (a preview of
  counting capitals).

Every record gains a `demo` object saying how its capability was found,
whether its keyword was bold, and whether this suite can test it. IDs are the
extractor's positional IDs, so they are provisional.

It also writes `capabilities.json`, an index of the capabilities in the
catalog: whether each is an extension, which capabilities it extends, its
schema and its pages. An extension's parents are read from its schema: the
spec requires a `$defs` entry, keyed by the parent's full name, for every
parent an extension declares.

Decisions in `requirements/review.json`, made by a person, override the
automatic testability and can mark a rule as not a rule.

Usage, from the repository root:

  uv run --no-project requirements/tools/build_catalog.py \
    --spec-root=<ucp checkout> --spec-commit=<commit> \
    --extractor-dir=<.github checkout>/org-tools/requirements-extractor/scripts
"""

import argparse
import collections
from collections.abc import Callable, Iterator
import contextlib
import dataclasses
import importlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from types import ModuleType
from typing import Any

DOCS_ROOT = PurePosixPath("docs/specification")
SHOPPING_DIR = DOCS_ROOT / "shopping"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "requirements" / "catalog"
DEFAULT_REVIEW_FILE = REPO_ROOT / "requirements" / "review.json"
REPORT_FILE = "extraction_report.json"
INDEX_FILE = "capabilities.json"
OPTIONS = ("capability-from-bullet-or-path", "count-capitals")

BOLD_KEYWORD_RE = re.compile(
  r"\*\*\s*(?:MUST\s+NOT|MUST|SHALL\s+NOT|SHALL|SHOULD\s+NOT|SHOULD|"
  r"NOT\s+RECOMMENDED|RECOMMENDED|REQUIRED|MAY|OPTIONAL)\s*\*\*"
)

# Whether this suite can test a rule. The suite plays the platform and calls a
# business over REST, so it can check what a business must do on a
# transport-neutral page or the REST binding page, and nothing else. A rule
# with no clear actor counts as testable, so coverage is not overstated.
BINDING_PAGES = {
  "rest.md": "rest",
  "mcp.md": "mcp",
  "a2a.md": "a2a",
  "embedded.md": "embedded",
}
UNTESTABLE_TRANSPORTS = {
  "mcp": "MCP binding page",
  "a2a": "A2A binding page",
  "embedded": "embedded protocol page",
}
TESTABLE_ACTORS = {
  "BUSINESS": "aimed at the business",
  None: "no actor detected",
  "IMPLEMENTATION": "implementation guidance",
  "EXTENSION": "attributed to the extension",
}
UNTESTABLE_ACTORS = {
  "PLATFORM": "aimed at the platform",
  "AGENT": "aimed at the agent",
  "HOST": "aimed at the host",
  "EMBEDDED_CHECKOUT": "aimed at the embedded checkout",
  "BUYER": "aimed at the buyer",
}
MUST_LEVELS = ("MUST", "MUST NOT")


def testability(record: dict[str, Any]) -> tuple[str, bool | None, str]:
  """Return a rule's transport, whether this suite can test it, and why.

  Returns None for testability when the rule's actor is not one this tool
  knows, which the caller reports as a problem.
  """
  transport = BINDING_PAGES.get(
    PurePosixPath(record["source"]["file"]).name, "core"
  )
  if transport in UNTESTABLE_TRANSPORTS:
    return transport, False, UNTESTABLE_TRANSPORTS[transport]
  actor = record["target_actor"]
  if actor in UNTESTABLE_ACTORS:
    return transport, False, UNTESTABLE_ACTORS[actor]
  if actor in TESTABLE_ACTORS:
    return transport, True, TESTABLE_ACTORS[actor]
  return transport, None, f"unknown actor {actor}"


@dataclasses.dataclass(frozen=True)
class PageLabel:
  """The capability label of one specification page.

  Attributes:
    capabilities: Capability names the page's rules belong to.
    source: How the label was found: "bullet", "path", "path (family)" or
      "unresolved".
    id_name: The name the extractor builds IDs from. For a folder of several
      capabilities this is the folder's shared prefix.

  """

  capabilities: tuple[str, ...]
  source: str
  id_name: str | None


@dataclasses.dataclass(frozen=True)
class CapabilitySchema:
  """What a capability's schema says about it.

  Attributes:
    file: The schema's path, relative to the spec root.
    defs_names: `$defs` keys that are capability names, in schema order.

  """

  file: str
  defs_names: tuple[str, ...]


def schema_capabilities(spec_root: Path) -> dict[str, CapabilitySchema]:
  """Return every capability a schema in the spec declares, by name."""
  schemas = {}
  for path in sorted((spec_root / "source" / "schemas").rglob("*.json")):
    try:
      document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
      continue
    name = document.get("name") if isinstance(document, dict) else None
    if isinstance(name, str) and name.startswith("dev.ucp."):
      schemas[name] = CapabilitySchema(
        file=path.relative_to(spec_root).as_posix(),
        defs_names=tuple(
          key for key in document.get("$defs", {}) if key.startswith("dev.ucp.")
        ),
      )
  return schemas


def parents_of(name: str, schemas: dict[str, CapabilitySchema]) -> list[str]:
  """Return the capabilities an extension extends, from its schema's `$defs`.

  A `$defs` key naming the capability itself is not a parent.
  """
  return sorted(
    key for key in schemas[name].defs_names if key in schemas and key != name
  )


def effective_level(record: dict[str, Any]) -> str | None:
  """Return a rule's level, as corrected by the review if it was."""
  return record["demo"].get("review", {}).get("level") or record["level"]


def is_active(record: dict[str, Any]) -> bool:
  """Whether a rule counts: false when the review found it is not a rule."""
  return not record["demo"].get("excluded")


def capability_index(
  schemas: dict[str, CapabilitySchema],
  labels: dict[str, PageLabel],
  records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
  """Describe every capability that has rules in the catalog.

  Rules the review found are not rules are left out of every count.

  Args:
    schemas: Capabilities declared by the schemas.
    labels: Each page's capability label.
    records: The catalog's records.

  Returns:
    One entry per capability, by name: its kind, the capabilities it
    extends and is extended by, its schema, and its pages with their rule
    counts.

  """
  records = [r for r in records if is_active(r)]
  per_page = collections.Counter(r["source"]["file"] for r in records)
  in_scope = sorted({c for r in records for c in r["capabilities"]})
  parents = {name: parents_of(name, schemas) for name in in_scope}
  entries = []
  for name in in_scope:
    pages = []
    for rel_path in sorted(labels):
      label = labels[rel_path]
      if name not in label.capabilities or not per_page[rel_path]:
        continue
      page = {"file": rel_path, "rules": per_page[rel_path]}
      shared = [c for c in label.capabilities if c != name]
      if shared:
        page["shared_with"] = shared
      pages.append(page)
    own = [r for r in records if name in r["capabilities"]]
    testable = [r for r in own if r["demo"]["testable"]]
    entries.append(
      {
        "name": name,
        "kind": "extension" if parents[name] else "capability",
        "extends": parents[name],
        "extended_by": sorted(e for e in in_scope if name in parents[e]),
        "schema": schemas[name].file,
        "rules": sum(page["rules"] for page in pages),
        "testable_rules": len(testable),
        "testable_must": sum(
          effective_level(r) in MUST_LEVELS for r in testable
        ),
        "pages": pages,
      }
    )
  return entries


def path_label(rel_path: str, names: set[str]) -> PageLabel:
  """Label a page from its file path, with hyphens read as underscores.

  - `<vertical>/extensions/<page>.md` belongs to `dev.ucp.<vertical>.<page>`;
  - a page named after a capability belongs to it
    (`shopping/catalog/search.md`);
  - a page in a capability's folder belongs to that capability
    (`shopping/cart/rest.md`);
  - a page in a folder of several capabilities belongs to all of them
    (`shopping/catalog/rest.md`).

  Every name produced is one a schema declares.

  Args:
    rel_path: Repository-relative POSIX path of the page.
    names: Capability names declared by the schemas.

  Returns:
    The label, with source "unresolved" when no rule applies.

  """
  parts = [
    part.replace("-", "_")
    for part in PurePosixPath(rel_path)
    .relative_to(DOCS_ROOT)
    .with_suffix("")
    .parts
  ]
  unresolved = PageLabel((), "unresolved", None)
  if len(parts) < 3:
    return unresolved
  vertical, folder, stem = parts[0], parts[1], parts[-1]
  if folder == "extensions":
    name = f"dev.ucp.{vertical}.{stem}"
    if len(parts) == 3 and name in names:
      return PageLabel((name,), "path", name)
    return unresolved
  exact = f"dev.ucp.{vertical}.{folder}.{stem}"
  if len(parts) == 3 and exact in names:
    return PageLabel((exact,), "path", exact)
  folder_name = f"dev.ucp.{vertical}.{folder}"
  if folder_name in names:
    return PageLabel((folder_name,), "path", folder_name)
  family = tuple(sorted(n for n in names if n.startswith(folder_name + ".")))
  if family:
    return PageLabel(family, "path (family)", folder_name)
  return unresolved


class DemoResolver:
  """Resolve each page's capability once: the bullet first, then the path."""

  def __init__(
    self, declared: Callable[[Path], str | None], names: set[str]
  ) -> None:
    """Initialize the resolver.

    Args:
      declared: The extractor's reader for a page's capability bullet.
      names: Capability names declared by the schemas.

    """
    self._declared = declared
    self._names = names
    self.labels: dict[str, PageLabel] = {}
    self.problems: list[str] = []

  def __call__(self, rel_path: str, abs_path: Path | None = None) -> str | None:
    """Return the name the extractor should build this page's IDs from."""
    if rel_path not in self.labels:
      self.labels[rel_path] = self._label(rel_path, abs_path)
    return self.labels[rel_path].id_name

  def _label(self, rel_path: str, abs_path: Path | None) -> PageLabel:
    declared = self._declared(abs_path) if abs_path is not None else None
    from_path = path_label(rel_path, self._names)
    if not declared:
      return from_path
    if declared not in self._names:
      self.problems.append(
        f"{rel_path}: the bullet names {declared}, which no schema declares"
      )
    if from_path.capabilities and declared not in from_path.capabilities:
      self.problems.append(
        f"{rel_path}: the bullet says {declared} but the file path says "
        + ", ".join(from_path.capabilities)
      )
    return PageLabel((declared,), "bullet", declared)


@contextlib.contextmanager
def replaced(module: ModuleType, name: str, value: Any) -> Iterator[None]:
  """Temporarily replace a module attribute."""
  original = getattr(module, name)
  setattr(module, name, value)
  try:
    yield
  finally:
    setattr(module, name, original)


def keyword_style(clause: str) -> str:
  """Return "bold" if the clause states its keyword in bold, else "capitals"."""
  return "bold" if BOLD_KEYWORD_RE.search(clause) else "capitals"


def git_commit(path: Path) -> tuple[str, bool]:
  """Return the commit a directory is at, and whether it has changes."""
  commit = subprocess.run(
    ["git", "-C", str(path), "rev-parse", "HEAD"],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()
  status = subprocess.run(
    ["git", "-C", str(path), "status", "--porcelain", "--", "."],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()
  return commit, bool(status)


def load_extractor(extractor_dir: Path) -> dict[str, ModuleType]:
  """Import the extractor modules from a `.github` checkout."""
  sys.path.insert(0, str(extractor_dir))
  return {
    name: importlib.import_module(f"requirements_extractor.{name}")
    for name in ("catalog", "classifier", "config")
  }


REVIEW_VERDICTS = ("rule", "not a rule")
REVIEW_OPTIONAL = ("level", "note", "needs_your_call")


def load_review(
  path: Path, problems: list[str]
) -> dict[tuple[str, str], dict[str, Any]]:
  """Load review decisions, keyed by page and text digest.

  The digest moves only when a rule's wording does, so a decision survives
  ID renumbering and lapses, visibly, when the rule is reworded. A sentence
  repeated word for word on one page shares a digest, so its copies share a
  key: their decisions must agree.

  Args:
    path: The review file; a missing file means no decisions.
    problems: Collects malformed entries and conflicting decisions.

  Returns:
    The decisions by (file, text_digest).

  """
  if not path.exists():
    return {}
  decisions = {}
  compared = ("verdict", "actor", "testable", "reason", "level")
  for entry in json.loads(path.read_text(encoding="utf-8"))["decisions"]:
    key = (entry["file"], entry["text_digest"])
    if entry["verdict"] not in REVIEW_VERDICTS:
      problems.append(f"review: unknown verdict for {entry['id_at_review']}")
    earlier = decisions.get(key)
    if earlier and any(earlier.get(k) != entry.get(k) for k in compared):
      problems.append(
        f"review: conflicting decisions for repeated wording "
        f"({earlier['id_at_review']}, {entry['id_at_review']})"
      )
    decisions.setdefault(key, entry)
  return decisions


def apply_review(record: dict[str, Any], decision: dict[str, Any]) -> None:
  """Record a review decision on a rule and let it override testability.

  A rule found not to be a rule stays in the catalog, flagged as excluded,
  so the decision is traceable; it is left out of every count.
  """
  review = {k: decision[k] for k in ("verdict", "actor", "testable", "reason")}
  review.update({k: decision[k] for k in REVIEW_OPTIONAL if k in decision})
  demo = record["demo"]
  demo["review"] = review
  if decision["verdict"] == "not a rule":
    demo["excluded"] = True
    demo["testable"] = False
    demo["testable_reason"] = "not a rule (reviewed): " + decision["reason"]
  else:
    demo["testable"] = decision["testable"]
    demo["testable_reason"] = "reviewed: " + decision["reason"]


def main(argv: list[str] | None = None) -> int:
  """Build the catalog, write it, and print a summary."""
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--spec-root", type=Path, required=True)
  parser.add_argument("--spec-commit", required=True)
  parser.add_argument("--extractor-dir", type=Path, required=True)
  parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
  parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW_FILE)
  args = parser.parse_args(argv)
  spec_root = args.spec_root.resolve()
  out = sys.stdout.write

  modules = load_extractor(args.extractor_dir.resolve())
  catalog, classifier, config = (
    modules["catalog"],
    modules["classifier"],
    modules["config"],
  )
  extractor_commit, extractor_dirty = git_commit(args.extractor_dir)
  schemas = schema_capabilities(spec_root)
  names = set(schemas)
  resolver = DemoResolver(config.declared_capability, names)
  config.SPEC_DIRS = [Path(SHOPPING_DIR)]

  original_find = classifier.find_keywords

  def find_all_keywords(text: str, keywords: tuple[str, ...]) -> list[Any]:
    return [
      dataclasses.replace(hit, bolded=True)
      for hit in original_find(text, keywords)
    ]

  with replaced(config, "resolve_capability", resolver):
    bold_only, _ = catalog.build(spec_root)
    with replaced(classifier, "find_keywords", find_all_keywords):
      requirements, report = catalog.build(spec_root)
    documents = catalog.catalog_documents(requirements, report, spec_root)
    report_document = catalog.report_document(requirements, report, spec_root)

  demo_header = {
    "spec_commit": args.spec_commit,
    "extractor_commit": extractor_commit,
    "options": list(OPTIONS),
    "ids": "provisional",
  }
  review_problems: list[str] = []
  reviews = load_review(args.review.resolve(), review_problems)
  used_reviews = set()
  pre_review = {}
  records = []
  for path, document in documents.items():
    label = resolver.labels[document["source_file"]]
    for record in document["requirements"]:
      if record["capabilities"]:
        record["capabilities"] = list(label.capabilities)
      transport, testable, reason = testability(record)
      record["demo"] = {
        "capability_source": label.source,
        "keyword_style": keyword_style(record["clause"]),
        "transport": transport,
        "testable": testable,
        "testable_reason": reason,
      }
      pre_review[record["id"]] = (testable, record["level"])
      key = (record["source"]["file"], record["text_digest"])
      if key in reviews:
        used_reviews.add(key)
        apply_review(record, reviews[key])
      records.append(record)
    documents[path] = dict(
      [
        *[(k, v) for k, v in document.items() if k != "requirements"],
        ("demo", demo_header),
        ("requirements", document["requirements"]),
      ]
    )

  # Checks. Anything here is unexpected and fails the run.
  problems = list(resolver.problems) + review_problems
  stale = sorted(
    reviews[key]["id_at_review"] for key in set(reviews) - used_reviews
  )
  if stale:
    problems.append(
      f"review: {len(stale)} decisions match no rule (reworded or "
      f"removed): {stale[:5]}"
    )
  unknown_actors = sorted(
    {r["target_actor"] for r in records if r["demo"]["testable"] is None},
    key=str,
  )
  if unknown_actors:
    problems.append(f"actors this tool does not know: {unknown_actors}")
  if extractor_dirty:
    problems.append(
      f"the extractor checkout has local changes ({extractor_commit})"
    )
  unresolved = [r["id"] for r in records if not r["capabilities"]]
  if unresolved:
    problems.append(
      f"{len(unresolved)} rules have no capability: {unresolved[:5]}"
    )
  unknown = sorted({c for r in records for c in r["capabilities"]} - names)
  if unknown:
    problems.append(f"capabilities no schema declares: {unknown}")
  duplicate_ids = [
    i for i, n in collections.Counter(r["id"] for r in records).items() if n > 1
  ]
  if duplicate_ids:
    problems.append(f"duplicate IDs: {duplicate_ids[:5]}")
  index = capability_index(schemas, resolver.labels, records)
  kinds = {entry["name"]: entry["kind"] for entry in index}
  for rel_path, label in sorted(resolver.labels.items()):
    in_extensions = PurePosixPath(rel_path).parent.name == "extensions"
    for name in label.capabilities:
      if in_extensions and kinds.get(name) != "extension":
        problems.append(
          f"{rel_path} is an extension page but {name} extends nothing"
        )
      if not in_extensions and kinds.get(name) == "extension":
        problems.append(f"{rel_path} is labelled with the extension {name}")

  # Write.
  out_dir = args.out_dir.resolve()
  if (out_dir / "shopping").exists():
    shutil.rmtree(out_dir / "shopping")
  for path, document in documents.items():
    target = out_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(catalog.serialize(document), encoding="utf-8")
  report_document = {**report_document, "demo": demo_header}
  (out_dir / REPORT_FILE).write_text(
    catalog.serialize(report_document), encoding="utf-8"
  )
  index_document = {
    "demo": demo_header,
    "extends_from": "each extension schema's $defs keys that name another "
    "capability",
    "capabilities": index,
  }
  (out_dir / INDEX_FILE).write_text(
    catalog.serialize(index_document), encoding="utf-8"
  )

  # Summary.
  styles = collections.Counter(r["demo"]["keyword_style"] for r in records)
  out(f"spec {args.spec_commit[:8]}, extractor {extractor_commit[:7]}\n")
  out(
    f"rules: {len(records)} ({styles['bold']} bold, "
    f"{styles['capitals']} capitals); bold-only run: {len(bold_only)}\n"
  )
  bold_digests = collections.Counter(r.text_digest for r in bold_only)
  new_digests = collections.Counter(r["text_digest"] for r in records)
  changed = bold_digests - new_digests
  out(
    "bold-only rules whose wording changed once capitals count: "
    f"{sum(changed.values())}\n"
  )
  bold_levels = {
    (r.source.file, r.text_digest): str(r.level) for r in bold_only
  }
  level_changes = [
    (r["id"], bold_levels[key], r["level"])
    for r in records
    if (key := (r["source"]["file"], r["text_digest"])) in bold_levels
    and bold_levels[key] != r["level"]
  ]
  out(f"rules whose level changed once capitals count: {len(level_changes)}\n")
  for change in level_changes[:10]:
    out(f"  {change[0]}: {change[1]} -> {change[2]}\n")

  out("\nby capability (bold / capitals):\n")
  by_capability = collections.defaultdict(collections.Counter)
  for record in records:
    key = " + ".join(record["capabilities"])
    by_capability[key][record["demo"]["keyword_style"]] += 1
  for key in sorted(by_capability):
    counts = by_capability[key]
    out(
      f"  {sum(counts.values()):4}  {key}  "
      f"({counts['bold']} / {counts['capitals']})\n"
    )

  out("\nby page:\n")
  per_page = collections.Counter(r["source"]["file"] for r in records)
  for rel_path in sorted(resolver.labels):
    label = resolver.labels[rel_path]
    short = PurePosixPath(rel_path).relative_to(SHOPPING_DIR)
    caps = ", ".join(
      c.removeprefix("dev.ucp.shopping.") for c in label.capabilities
    )
    out(
      f"  {per_page[rel_path]:4}  {short!s:28} "
      f"{label.source:14} {caps or '-'}\n"
    )

  out("\ncapability index:\n")
  for entry in index:
    short_name = entry["name"].removeprefix("dev.ucp.shopping.")
    if entry["kind"] == "extension":
      relation = "extends " + ", ".join(
        e.removeprefix("dev.ucp.shopping.") for e in entry["extends"]
      )
    else:
      relation = "extended by " + (
        ", ".join(
          e.removeprefix("dev.ucp.shopping.") for e in entry["extended_by"]
        )
        or "-"
      )
    out(
      f"  {short_name:16} {entry['kind']:10} {relation:44} "
      f"{len(entry['pages'])} pages, {entry['rules']} rules\n"
    )
  in_scope = set(kinds)
  elsewhere = sorted(
    name
    for name in names - in_scope
    if set(parents_of(name, schemas)) & in_scope
  )
  if elsewhere:
    out(
      "  not in scope (outside the shopping folder) but extending a shopping "
      "capability: " + ", ".join(elsewhere) + "\n"
    )

  out("\ntestable by this suite (REST, against a business):\n")
  out(f"  {'capability':16} {'rules':>5} {'testable':>8} {'MUST':>5}\n")
  for entry in index:
    out(
      f"  {entry['name'].removeprefix('dev.ucp.shopping.'):16} "
      f"{entry['rules']:5} {entry['testable_rules']:8} "
      f"{entry['testable_must']:5}\n"
    )
  active = [r for r in records if is_active(r)]
  testable_records = [r for r in active if r["demo"]["testable"]]
  out(
    f"  {'unique rules':16} {len(active):5} {len(testable_records):8} "
    f"{sum(effective_level(r) in MUST_LEVELS for r in testable_records):5}\n"
  )
  for flag, title in ((True, "testable"), (False, "not testable")):
    reasons = collections.Counter(
      "reviewed" if "review" in r["demo"] else r["demo"]["testable_reason"]
      for r in active
      if r["demo"]["testable"] is flag
    )
    out(
      f"  {title}: "
      + ", ".join(f"{reason} {n}" for reason, n in reasons.most_common())
      + "\n"
    )

  reviewed = [r for r in records if "review" in r["demo"]]
  if reviewed:
    before = [t for t, _ in pre_review.values() if t]
    before_must = sum(
      bool(t) and level in MUST_LEVELS for t, level in pre_review.values()
    )
    flips = collections.Counter(
      (bool(pre_review[r["id"]][0]), r["demo"]["testable"])
      for r in reviewed
      if is_active(r)
    )
    actor_fixes = sum(
      r["demo"]["review"]["actor"] != (r["target_actor"] or "none")
      for r in reviewed
    )
    calls = sum(
      bool(r["demo"]["review"].get("needs_your_call")) for r in reviewed
    )
    out(
      f"\nreview ({args.review.name}): {len(reviewed)} decisions applied; "
      f"{len(records) - len(active)} not a rule; {calls} need your call\n"
    )
    out(
      f"  testable before review: {len(before)} ({before_must} MUST); "
      f"after: {len(testable_records)} "
      f"({sum(effective_level(r) in MUST_LEVELS for r in testable_records)}"
      " MUST)\n"
    )
    out(
      f"  testable -> not testable: {flips[(True, False)]}; "
      f"not testable -> testable: {flips[(False, True)]}; "
      f"actor set or corrected: {actor_fixes}\n"
    )

  out(
    f"\nwrote {len(documents)} page files, {REPORT_FILE} and {INDEX_FILE} "
    f"to {out_dir}\n"
  )
  if problems:
    out("\nPROBLEMS:\n" + "".join(f"  - {p}\n" for p in problems))
    return 1
  out(
    "checks: every rule has a capability, all names are declared, "
    "no conflicts, every extension page belongs to an extension\n"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())

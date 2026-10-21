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
whether its keyword was bold, and whether this suite can test it.

IDs are stable. Each rule's ID comes from `requirements.lock.json`, beside
the catalog, so it no longer changes when a rule is added above it; a
reworded rule keeps its ID, and a removed rule's ID is retired, never
reused. The extractor's positional ID is kept as `demo.positional_id`.

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

With `--check`, nothing is written, and the run fails if any output, the
lock included, would change.
"""

import argparse
import collections
from collections.abc import Callable, Iterator
import contextlib
import copy
import dataclasses
import difflib
import hashlib
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
LOCK_FILE = "requirements.lock.json"
OPTIONS = ("capability-from-bullet-or-path", "count-capitals")

# Stable IDs: one lock entry per ID ever issued, in ID order.
LOCK_VERSION = 1
LOCK_ABOUT = (
  "Stable IDs for the demo catalog: one entry for every ID ever issued. An "
  "ID never changes, and a retired ID's number is never issued again. See "
  "requirements/README.md."
)
LOCK_FIELDS = (
  "id",
  "file",
  "text_digest",
  "context",
  "text",
  "previous_digests",
  "status",
  "minted_at",
  "retired_at",
)
LOCK_STATUSES = ("active", "retired")
ID_NUMBER_WIDTH = 4
# The catalog schema's pattern for a requirement ID.
ID_RE = re.compile(r"^REQ-[A-Z0-9-]+-[0-9]{2,}$")
# How alike a rule's wording must be to that of a rule missing from the same
# page for the two to count as one rule, reworded.
REWORD_SIMILARITY = 0.85
# The summary lists changed IDs one by one when there are at most this many.
LIST_LIMIT = 20

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
    for name in ("catalog", "classifier", "config", "identity")
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


@dataclasses.dataclass(frozen=True)
class LockRule:
  """What the lock needs to know about one rule.

  Attributes:
    prefix: The ID prefix for the rule's page, such as `REQ-SHOPPING-CART`.
    file: The rule's page.
    text_digest: The rule's text digest.
    context: The rule's normalized compound parent, if it has one.
    text: The rule's normalized wording.
    position: Where the rule sits on its page: its first line, then the
      extractor's document order.

  """

  prefix: str
  file: str
  text_digest: str
  context: str | None
  text: str
  position: tuple[int, int]


@dataclasses.dataclass
class LockResult:
  """The outcome of giving every rule a stable ID.

  Attributes:
    ids: Each rule's stable ID, in the order the rules were given.
    entries: Every lock entry after the run, in ID order.
    kept: IDs whose rule was found word for word.
    carried: IDs whose rule was found reworded, each with the similarity and
      the wording before.
    minted: New IDs.
    retired: IDs whose rule is gone.
    restored: Retired IDs whose rule is back, word for word.

  """

  ids: list[str] = dataclasses.field(default_factory=list)
  entries: list[dict[str, Any]] = dataclasses.field(default_factory=list)
  kept: list[str] = dataclasses.field(default_factory=list)
  carried: list[tuple[str, float, str]] = dataclasses.field(
    default_factory=list
  )
  minted: list[str] = dataclasses.field(default_factory=list)
  retired: list[str] = dataclasses.field(default_factory=list)
  restored: list[str] = dataclasses.field(default_factory=list)


def split_id(rule_id: str) -> tuple[str, int]:
  """Split a stable ID into its prefix and its number."""
  prefix, _, number = rule_id.rpartition("-")
  return prefix, int(number)


def match_text(context: str | None, text: str) -> str:
  """Return the wording compared when looking for a reworded rule.

  The context is included because the wording alone can be a single word:
  the cells of the checkout obligation matrix read "must" or "may".
  """
  return f"{context}\n{text}" if context else text


def mint_order(rule: LockRule) -> tuple[Any, ...]:
  """Return the order new IDs go out in: core page first, then page order."""
  return (
    rule.prefix,
    PurePosixPath(rule.file).name != "index.md",
    rule.file,
    rule.position,
  )


def assign_stable_ids(
  rules: list[LockRule], lock: list[dict[str, Any]], spec_commit: str
) -> LockResult:
  """Give every rule a stable ID from the lock, issuing new IDs as needed.

  Four passes:

  1. Exact: a rule takes the ID of an entry on the same page with the same
     text digest. Copies of one sentence on a page take that wording's IDs
     oldest first, in page order. A retired entry can match, which restores
     its ID.
  2. Reworded: a rule still without an ID takes the ID of an unclaimed
     active entry on the same page whose wording is at least
     `REWORD_SIMILARITY` alike, the most alike pairs first. The entry keeps
     its old digest in `previous_digests`.
  3. New: every rule still without an ID gets the next number for its
     prefix, in `mint_order`.
  4. Retired: an active entry no rule claimed is retired. Its number is
     never issued again.

  Args:
    rules: The catalog's rules.
    lock: The lock's entries. They are not modified.
    spec_commit: The specification commit, recorded on new and retired
      entries.

  Returns:
    The IDs, the updated entries and what changed.

  """
  entries = {entry["id"]: copy.deepcopy(entry) for entry in lock}
  ids: list[str | None] = [None] * len(rules)
  claimed: set[str] = set()
  result = LockResult()
  page_order = sorted(
    range(len(rules)), key=lambda i: (rules[i].file, rules[i].position)
  )

  # 1. Exact.
  by_wording = collections.defaultdict(list)
  for rule_id in sorted(entries, key=split_id):
    entry = entries[rule_id]
    by_wording[entry["file"], entry["text_digest"]].append(rule_id)
  for i in page_order:
    for rule_id in by_wording[rules[i].file, rules[i].text_digest]:
      if rule_id in claimed:
        continue
      claimed.add(rule_id)
      ids[i] = rule_id
      entry = entries[rule_id]
      if entry["status"] == "retired":
        entry["status"], entry["retired_at"] = "active", None
        result.restored.append(rule_id)
      else:
        result.kept.append(rule_id)
      break

  # 2. Reworded.
  open_entries = collections.defaultdict(list)
  for rule_id in sorted(entries, key=split_id):
    entry = entries[rule_id]
    if rule_id not in claimed and entry["status"] == "active":
      open_entries[entry["file"]].append(rule_id)
  pairs = []
  for rank, i in enumerate(page_order):
    if ids[i] is not None:
      continue
    wording = match_text(rules[i].context, rules[i].text)
    for rule_id in open_entries[rules[i].file]:
      entry = entries[rule_id]
      matcher = difflib.SequenceMatcher(
        None,
        match_text(entry["context"], entry["text"]),
        wording,
        autojunk=False,
      )
      if (
        matcher.real_quick_ratio() >= REWORD_SIMILARITY
        and matcher.quick_ratio() >= REWORD_SIMILARITY
        and (similarity := matcher.ratio()) >= REWORD_SIMILARITY
      ):
        pairs.append((-similarity, split_id(rule_id), rank, i, rule_id))
  for negative_similarity, _, _, i, rule_id in sorted(pairs):
    if ids[i] is not None or rule_id in claimed:
      continue
    claimed.add(rule_id)
    ids[i] = rule_id
    entry = entries[rule_id]
    result.carried.append((rule_id, -negative_similarity, entry["text"]))
    entry["previous_digests"].append(entry["text_digest"])
    entry["text_digest"] = rules[i].text_digest
    entry["context"] = rules[i].context
    entry["text"] = rules[i].text

  # 3. New.
  highest: collections.Counter[str] = collections.Counter()
  for rule_id in entries:
    prefix, number = split_id(rule_id)
    highest[prefix] = max(highest[prefix], number)
  unmatched = [i for i in range(len(rules)) if ids[i] is None]
  for i in sorted(unmatched, key=lambda i: mint_order(rules[i])):
    rule = rules[i]
    highest[rule.prefix] += 1
    rule_id = f"{rule.prefix}-{highest[rule.prefix]:0{ID_NUMBER_WIDTH}d}"
    entries[rule_id] = {
      "id": rule_id,
      "file": rule.file,
      "text_digest": rule.text_digest,
      "context": rule.context,
      "text": rule.text,
      "previous_digests": [],
      "status": "active",
      "minted_at": spec_commit,
      "retired_at": None,
    }
    claimed.add(rule_id)
    ids[i] = rule_id
    result.minted.append(rule_id)

  # 4. Retired.
  for rule_id in sorted(entries, key=split_id):
    entry = entries[rule_id]
    if rule_id not in claimed and entry["status"] == "active":
      entry["status"], entry["retired_at"] = "retired", spec_commit
      result.retired.append(rule_id)

  result.ids = [rule_id or "" for rule_id in ids]
  result.entries = [entries[k] for k in sorted(entries, key=split_id)]
  return result


def load_lock(
  path: Path,
  digest_of: Callable[[str | None, str], str],
  problems: list[str],
) -> list[dict[str, Any]]:
  """Load the lock's entries and check that they are well formed.

  Args:
    path: The lock file. A missing file means no ID has been issued yet.
    digest_of: Computes a text digest from a context and a wording.
    problems: Collects anything malformed.

  Returns:
    The entries, as stored.

  """
  if not path.exists():
    return []
  document = json.loads(path.read_text(encoding="utf-8"))
  if document.get("lock_version") != LOCK_VERSION:
    problems.append(
      f"lock: unsupported lock_version {document.get('lock_version')!r}"
    )
    return []
  entries = document.get("entries", [])
  seen = set()
  for entry in entries:
    rule_id = str(entry.get("id"))
    if sorted(entry) != sorted(LOCK_FIELDS):
      problems.append(f"lock: {rule_id} does not have the expected fields")
      continue
    if not ID_RE.match(rule_id) or rule_id in seen:
      problems.append(f"lock: {rule_id} is malformed or repeated")
    seen.add(rule_id)
    retired = entry["status"] == "retired"
    if entry["status"] not in LOCK_STATUSES or retired != bool(
      entry["retired_at"]
    ):
      problems.append(f"lock: {rule_id} has an inconsistent status")
    if digest_of(entry["context"], entry["text"]) != entry["text_digest"]:
      problems.append(f"lock: {rule_id}'s wording does not match its digest")
  return entries


def lock_problems(lock: list[dict[str, Any]], result: LockResult) -> list[str]:
  """Check what giving out stable IDs must never do.

  Every ID ever issued stays in the lock; a new ID's number is above every
  number its prefix has used, retired ones included; and every rule has an
  ID that fits the catalog schema's pattern and an active entry of its own.
  """
  problems = []
  dropped = sorted({e["id"] for e in lock} - {e["id"] for e in result.entries})
  if dropped:
    problems.append(f"lock: IDs dropped: {dropped[:5]}")
  highest: collections.Counter[str] = collections.Counter()
  for entry in lock:
    prefix, number = split_id(entry["id"])
    highest[prefix] = max(highest[prefix], number)
  reused = [
    rule_id
    for rule_id in result.minted
    if split_id(rule_id)[1] <= highest[split_id(rule_id)[0]]
  ]
  if reused:
    problems.append(f"lock: numbers issued again: {reused[:5]}")
  malformed = [rule_id for rule_id in result.ids if not ID_RE.match(rule_id)]
  if malformed:
    problems.append(f"IDs that do not fit the schema: {malformed[:5]}")
  active = sum(entry["status"] == "active" for entry in result.entries)
  if active != len(result.ids):
    problems.append(f"lock: {active} active IDs for {len(result.ids)} rules")
  return problems


def lock_document(entries: list[dict[str, Any]]) -> dict[str, Any]:
  """Return the lock file's contents, each entry's fields in a fixed order."""
  return {
    "about": LOCK_ABOUT,
    "lock_version": LOCK_VERSION,
    "entries": [{field: e[field] for field in LOCK_FIELDS} for e in entries],
  }


def without_demo(document: dict[str, Any]) -> dict[str, Any]:
  """Return a catalog document without the demo's additions."""
  return {
    **{k: v for k, v in document.items() if k != "demo"},
    "requirements": [
      {k: v for k, v in record.items() if k != "demo"}
      for record in document["requirements"]
    ],
  }


def main(argv: list[str] | None = None) -> int:
  """Build the catalog, write it, and print a summary."""
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--spec-root", type=Path, required=True)
  parser.add_argument("--spec-commit", required=True)
  parser.add_argument("--extractor-dir", type=Path, required=True)
  parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
  parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW_FILE)
  parser.add_argument(
    "--check",
    action="store_true",
    help="write nothing; fail if any output, the lock included, would change",
  )
  args = parser.parse_args(argv)
  spec_root = args.spec_root.resolve()
  out_dir = args.out_dir.resolve()
  out = sys.stdout.write

  modules = load_extractor(args.extractor_dir.resolve())
  catalog, classifier, config, identity = (
    modules["catalog"],
    modules["classifier"],
    modules["config"],
    modules["identity"],
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
    "ids": f"stable, from {LOCK_FILE}",
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
        "positional_id": record["id"],
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

  # Stable IDs. A malformed lock stops the run before anything is written.
  def digest_of(context: str | None, text: str) -> str:
    joined = identity.DIGEST_SEPARATOR.join((context or "", text))
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return digest[: config.ID_DIGEST_LENGTH]

  lock_path = out_dir / LOCK_FILE
  lock_load_problems: list[str] = []
  lock = load_lock(lock_path, digest_of, lock_load_problems)
  if lock_load_problems:
    out("PROBLEMS:\n" + "".join(f"  - {p}\n" for p in lock_load_problems))
    return 1
  rules = [
    LockRule(
      prefix=f"{config.READABLE_ID_PREFIX}-"
      + config.capability_slug(resolver.labels[r["source"]["file"]].id_name),
      file=r["source"]["file"],
      text_digest=r["text_digest"],
      context=identity.normalize(r["compound_parent"])
      if r["compound_parent"]
      else None,
      text=r["normalized"],
      position=(r["source"]["start_line"], r["document_order"]),
    )
    for r in records
  ]
  assigned = assign_stable_ids(rules, lock, args.spec_commit)
  for record, stable_id in zip(records, assigned.ids, strict=True):
    record["id"] = stable_id
  stable_ids = {r["demo"]["positional_id"]: r["id"] for r in records}
  for entry in report_document["diagnostics"].get("duplicate_requirements", []):
    entry["ids"] = [stable_ids.get(i, i) for i in entry["ids"]]

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
  problems.extend(lock_problems(lock, assigned))
  for path, document in documents.items():
    problems.extend(
      f"{path}: {error}" for error in catalog.validate(without_demo(document))
    )
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

  # Write, or with --check only compare.
  report_document = {**report_document, "demo": demo_header}
  index_document = {
    "demo": demo_header,
    "extends_from": "each extension schema's $defs keys that name another "
    "capability",
    "capabilities": index,
  }
  outputs = {
    out_dir / path: catalog.serialize(document)
    for path, document in documents.items()
  }
  outputs[out_dir / REPORT_FILE] = catalog.serialize(report_document)
  outputs[out_dir / INDEX_FILE] = catalog.serialize(index_document)
  outputs[lock_path] = catalog.serialize(lock_document(assigned.entries))
  pages_dir = out_dir / "shopping"
  old_pages = set(pages_dir.rglob("*.json")) if pages_dir.exists() else set()
  out_of_date = sorted(
    (old_pages - set(outputs))
    | {
      path
      for path, text in outputs.items()
      if not path.exists() or path.read_text(encoding="utf-8") != text
    }
  )
  if not args.check:
    if pages_dir.exists():
      shutil.rmtree(pages_dir)
    for path, text in outputs.items():
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(text, encoding="utf-8")

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
      (bool(pre_review[r["demo"]["positional_id"]][0]), r["demo"]["testable"])
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

  statuses = collections.Counter(e["status"] for e in assigned.entries)
  out(
    f"\nstable IDs ({LOCK_FILE}): kept {len(assigned.kept)}, carried over "
    f"{len(assigned.carried)}, new {len(assigned.minted)}, retired "
    f"{len(assigned.retired)}, restored {len(assigned.restored)}\n"
    f"  the lock holds {len(assigned.entries)} IDs: {statuses['active']} "
    f"active, {statuses['retired']} retired\n"
  )
  if assigned.minted:
    by_prefix = collections.Counter(split_id(i)[0] for i in assigned.minted)
    out(
      "  new, by prefix: "
      + ", ".join(f"{p} {n}" for p, n in sorted(by_prefix.items()))
      + "\n"
    )
  where = {
    e["id"]: e["file"].removeprefix(f"{SHOPPING_DIR}/")
    for e in assigned.entries
  }
  for record in records:
    where[record["id"]] += f":{record['source']['start_line']}"
  for title, listed in (
    ("new", assigned.minted),
    ("retired", assigned.retired),
    ("restored", assigned.restored),
  ):
    if len(listed) <= LIST_LIMIT:
      for rule_id in listed:
        out(f"  {title}: {rule_id} at {where[rule_id]}\n")
  wording = {e["id"]: e["text"] for e in assigned.entries}
  for rule_id, similarity, before in assigned.carried:
    out(
      f"  carried over: {rule_id} at {where[rule_id]}, {similarity:.0%} "
      "alike; check it is the same rule\n"
      f"    was: {before[:100]}\n    now: {wording[rule_id][:100]}\n"
    )

  out(
    f"\n{'would write' if args.check else 'wrote'} {len(documents)} page "
    f"files, {REPORT_FILE}, {INDEX_FILE} and {LOCK_FILE} to {out_dir}; "
    f"{len(out_of_date)} files {'differ' if args.check else 'changed'}\n"
  )
  if args.check and out_of_date:
    out("check: nothing was written; out of date:\n")
    for path in out_of_date[:LIST_LIMIT]:
      out(f"  - {path.relative_to(out_dir)}\n")
    if len(out_of_date) > LIST_LIMIT:
      out(f"  - and {len(out_of_date) - LIST_LIMIT} more\n")
  elif args.check:
    out("check: every output is up to date\n")
  if problems:
    out("\nPROBLEMS:\n" + "".join(f"  - {p}\n" for p in problems))
    return 1
  if args.check and out_of_date:
    return 1
  out(
    "checks: every rule has a capability and its own stable ID, all names "
    "are declared, no conflicts, every extension page belongs to an "
    "extension, every page fits the catalog schema\n"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())

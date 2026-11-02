# Shopping requirements catalog (demo)

> **Demo only, not for merge.** This catalog is generated from a pinned copy of
> the specification with two options the requirements extractor doesn't have
> yet. Its IDs are stable within the demo (see [Stable IDs](#stable-ids)), but
> they aren't official IDs.

## Contents

| Path                             | What                                                                                  |
| -------------------------------- | ------------------------------------------------------------------------------------- |
| `catalog/shopping/**/*.json`     | One file per specification page, with every rule on that page                         |
| `catalog/capabilities.json`      | Each capability: extension or not, what it extends, its schema, pages and rule counts |
| `catalog/requirements.lock.json` | Every rule ID ever issued, with the wording it was issued for                         |
| `catalog/history.json`           | The releases read, the rules only older releases state, and rules that left the pages |
| `catalog/extraction_report.json` | The extractor's diagnostics for the run                                               |
| `releases.json`                  | The releases the history is read from, at pinned commits, and where their pages were  |
| `review.json`                    | A person's decisions on 199 rules the automatic pass couldn't settle                  |
| `findings.md`                    | What linking the 63 shopping tests to the catalog found: claims, spec and test gaps   |
| `tools/build_catalog.py`         | Regenerates the `catalog/` folder from the inputs below                               |
| `tools/suggest_rules.py`         | Suggests rules for each test, in a worksheet under `reports/`, which isn't committed  |

## How it was built

- **Specification:** `Universal-Commerce-Protocol/ucp` at `f2626c28`, every
  page under `docs/specification/shopping/`, including `extensions/`.
- **Extractor:** `org-tools/requirements-extractor` from
  `Universal-Commerce-Protocol/.github` PR #76, at `1687b56` (version 0.4.0).
- **Two preview options:**
  - every page gets a capability from its `Capability Name` bullet, or else
    from its file path (`extensions/<page>.md` belongs to
    `dev.ucp.shopping.<page>`; a shared catalog page belongs to both catalog
    capabilities);
  - upper-case RFC 2119 keywords count even when they aren't bold.
- **Extensions:** an extension's parents come from its schema's `$defs` keys
  that name another capability.
- **Testable:** this suite plays the platform and calls a business over REST,
  so a rule is testable when it sits on a transport-neutral or REST page and
  is aimed at the business, as corrected by `review.json`.
- **Review:** decisions are keyed by page and `text_digest`, not by ID, so a
  reworded rule needs a fresh decision: a decision whose wording no longer
  matches a rule fails the build.

Each record keeps the extractor's fields, with `id` holding the stable ID;
everything the demo adds is in its `demo` object, including the extractor's
positional ID as `demo.positional_id`.

## Stable IDs

Each rule's ID, such as `REQ-SHOPPING-CART-0007`, comes from
`catalog/requirements.lock.json`, so it doesn't change when the page around it
does. The prefix comes from the page's capability (`SHOPPING-CATALOG` for the
shared catalog pages), followed by a four-digit number. A lock entry records
the rule's page, its wording and digest, any earlier digests, whether it is
active or retired, and the specification commits it was issued and retired
at.

Each run matches the rules it finds against the lock, page by page:

1. **Same wording:** a rule with the same page and `text_digest` as an entry
   keeps that entry's ID. Copies of one sentence on a page take that
   wording's IDs oldest first, in page order. A retired ID whose wording comes
   back is restored.
2. **Reworded:** a rule left over takes the ID of a still-unmatched active
   entry on the same page whose wording is at least 85% alike, most alike
   first. The entry keeps its old digests in `previous_digests`, and the run
   lists the rule as carried over so a person can confirm it's the same rule.
3. **New:** a rule still left over gets the next number for its prefix.
4. **Retired:** an active entry that matched nothing is marked `retired`. Its
   number is never issued again. A rule only older releases state also has a
   retired ID (see [Release history](#release-history)).

A rule that moves to another page is retired on the old page and gets a new
ID on the new one. If one of two identical sentences on a page is removed, the
copy that stays keeps the older ID, whichever copy was removed.

These IDs are stable within this demo only. Official IDs will be issued in
the `ucp` repository, in a different order. Every lock entry records its
`text_digest`, so demo IDs can be mapped to official ones.

## Release history

The catalog also records which releases state each rule, so that a server
can be measured against the release it implements. `releases.json` lists the
releases, each at a pinned commit of its release branch: 2026-01-11,
2026-01-23, 2026-04-08 and 2026-08-25. The build reads each one's shopping
pages from a clone of the specification repository. The releases before
2026-08-25 have no shopping folder, so `releases.json` gives each of their
shopping pages its path today; its rules then get the capability and
testability that today's page gives them.

Releases are matched newest first, each against the wording that each rule
has in the next newer version, starting with the pinned specification:

1. the same wording on the same page, then on another page of the same
   capability;
2. the same wording under another list stem, or none, for a wording of at
   least five words (2026-01-11 has no stem for some lists);
3. a wording at least 85% alike, on the same page, then on another page of the
   same capability.

A release's rule that matches nothing is one of two things:

- **Moved out of the shopping pages:** the pinned specification states it on
  another page, such as `embedded-protocol.md`. It isn't in the catalog.
- **Removed:** only older releases state it. It gets a stable ID of its own,
  retired in the lock from the start, and `catalog/history.json` keeps its
  record: wording, level, capability and page.

Each rule's `demo.since` is the first version that states it, and
`demo.releases` says how each release states it: the page and line as the
release names them, the level, the digest, and the wording when it differs.
A removed rule also has `demo.until`, the first version that doesn't state
it. The build lists every match on a reworded wording for a person to check.
If the review finds that the meaning changed, a split in `review.json` makes
the older wording a removed rule of its own:

```json
{
  "splits": [{ "id": "REQ-SHOPPING-…", "before": "2026-08-25", "reason": "…" }]
}
```

A test may cite a removed rule; the marker check warns which releases state
it. The coverage report counts the pinned specification's rules as before,
and adds a view by release.

One limit: the pinned specification renders the shared REST header rules
(`UCP-Agent` and `Idempotency-Key`) with the `header_requirements` macro,
which the extractor reads as plain text. The catalog has no rule for them on
the checkout, cart, catalog and order REST pages, and the history counts the
older releases' copies as moved out, because the lodging pages still state
them in full.

## Linking tests to rules

Each shopping test says which rules it checks, with one marker from
`framework/decorators.py`:

- `@spec_assert("REQ-SHOPPING-CHECKOUT-0133", ...)`: the rules its assertions
  check, by stable ID, each with a comment giving the rule's wording. A rule
  they check only in part is wrapped in `partly(id, gap)`, where the gap says
  what the test leaves unchecked;
- `@no_rule("...")`: the spec describes the behaviour without an RFC 2119
  keyword, so there is no rule to cite;
- `@scenario("...")`: the test checks a store's own data, such as a
  free-shipping threshold, rather than a rule;
- `@outside_catalog("...")`: the test checks a rule on a specification page
  this catalog doesn't cover, such as the core overview page;
- `@older_version("2026-04-08", "...")`: the test checks behaviour from an
  older release that this specification has changed, so it needs updating.

For example, a totals test that checks one rule fully and another in part:

```python
@spec_assert(
  partly(
    "REQ-SHOPPING-CHECKOUT-0016",  # line total MUST be price x quantity
    "a quantity of 1 only, so scale and rounding aren't checked",
  ),
  "REQ-SHOPPING-CHECKOUT-0134",  # Exactly one "total" MUST be present
)
```

`framework/requirements.py` looks rules up in the catalog. Before any test
runs, the runner checks every shopping test's marker against it, and so does
pytest:

- **Problems** stop the run: a test with no marker, or an ID that isn't in the
  catalog, was retired, or the review found isn't a rule.
- **Warnings** don't: a rule this suite can't test, or one outside the
  capabilities the test requires.

`--dry-run` runs the check without a server. `--requirements_check=warn`
(`--requirements-check=warn` with pytest) reports problems without stopping,
and `off` skips the check. Unit tests for the markers, the loader, the check
and the report are in `tests/`: run `python -m pytest tests`.

## Rules the server's version doesn't state

A server advertises the release each of its capabilities implements, such as
`2026-04-08`, and the catalog records which releases state each rule. Before
a `spec_assert` test runs, `framework/versions.py` compares the rules it
cites with the version the server advertises for their capability (or its
profile's version, when the capability has none). If that version states
none of them, the test doesn't apply and is skipped with a reason that says
why:

```text
not applicable: the server implements 2026-04-08, which doesn't state
REQ-SHOPPING-FULFILLMENT-0012 (introduced in 2026-08-25)
```

A rule the server's version removed reads `removed in 2026-04-08`, and one
only the draft states, `not yet in a release`. A test that cites one stated
rule among others runs. Tests without rules (`no_rule`, `scenario` and the
rest) always run, and so does everything when the server's profile can't be
fetched.

A version the catalog doesn't know is read as the newest release that isn't
newer than it. A version newer than every release, a version older than all
of them, and a server that advertises no version are read as the draft, the
catalog's own version.

The runner records such a skip as **not applicable**, an outcome of its own,
and lists it apart from the other skips in its summary. In the coverage
report a rule the server's version doesn't state is not applicable too,
whatever its tests did: it neither passes nor fails, and it leaves the
denominator of the results. Coverage is unaffected: it is measured against
the pinned specification, as before.

## Coverage report

`--requirements_report=<dir>` makes the runner write a coverage report to a
directory. With `--dry-run`, the report reads only the catalog and the tests'
markers, so it needs no server, and it says which rules have a test:

```bash
uv run ucp-conformance --suite=shopping --dry-run \
  --requirements_report=requirements/reports
```

Without `--dry-run`, the runner first checks that it can write the report,
then runs the tests, then writes the report with each test's outcome and
each rule's result:

```bash
uv run ucp-conformance --suite=shopping --server_url=http://localhost:8182 \
  --requirements_report=requirements/reports
```

It writes the same report three ways:

| File          | For                                                                                              |
| ------------- | ------------------------------------------------------------------------------------------------ |
| `matrix.md`   | Reading on GitHub                                                                                |
| `matrix.html` | Reading in a browser: one page, with no scripts and nothing loaded from elsewhere                |
| `matrix.json` | Tools: every count, every rule with its tests and result, every test with its marker and outcome |

The Markdown and HTML pages hold:

- **Summary:** for each capability, then each extension, and for all rules
  counted once: the rules, the testable rules, the testable MUST and MUST NOT
  rules, and how many of those a test checks fully, or only in part;
- **By release:** for each release and the draft, the testable MUST and MUST
  NOT rules it states, removed rules included, and how many a test checks;
- **Results,** after a run: for each capability, how many of its testable
  rules with a test passed, failed, weren't run or don't apply to the
  server's version, then the rules that failed, the rules whose tests didn't
  run and the rules the server's version doesn't state, with their tests'
  messages;
- **A section per capability:** the rules a test checks fully, then those
  checked only in part, each test with its gap, with their results after a
  run; then the testable MUST and MUST NOT rules without a test, which is the
  to-do list; the other testable rules and the rules this suite can't test
  are folded away;
- **Tests:** every shopping test, grouped by marker, with its outcome after a
  run;
- **Notes:** what the counts mean, with a link to `findings.md`.

A rule counts as covered when a `spec_assert` marker cites it as fully
checked. A rule cited only with `partly` counts as checked in part, not
covered, and in `matrix.json` each rule's `coverage` is `full`, `partial` or
`none`. After a run, a rule passed when at least one of the tests that cite
it passed and none failed; it failed when any of them failed or hit an
error; and it wasn't run when they were all skipped or didn't run. A rule
the version the server implements doesn't state is not applicable instead
(see [Rules the server's version doesn't state](#rules-the-servers-version-doesnt-state)).
Tests that check the rule only in part count here too, so a pass shows only
what their assertions check. `findings.md` explains each partial claim.
Outcomes come from the runner, so a pytest run doesn't record them.

The report covers the tests the run selects, with `--suite` and `-k`.
`requirements/reports/` isn't committed.

## Numbers at `f2626c28`

- 510 rules extracted: 395 bold and 115 found through capitals. The review
  found 3 aren't rules, leaving 507.
- 217 are testable by this suite, 104 of them MUST or MUST NOT.

| Capability                        | Rules | Testable | Testable MUST |
| --------------------------------- | ----: | -------: | ------------: |
| `dev.ucp.shopping.checkout`       |   250 |       78 |            37 |
| `dev.ucp.shopping.cart`           |    65 |       21 |             8 |
| `dev.ucp.shopping.catalog.lookup` |    61 |       38 |            16 |
| `dev.ucp.shopping.catalog.search` |    55 |       30 |            11 |
| `dev.ucp.shopping.fulfillment`    |    55 |       37 |            16 |
| `dev.ucp.shopping.order`          |    40 |       24 |            17 |
| `dev.ucp.shopping.buyer_consent`  |    17 |        5 |             4 |
| `dev.ucp.shopping.discount`       |     9 |        7 |             4 |

The 46 rules on the shared catalog pages count under both catalog
capabilities.

This catalog covers rules in the specification pages. Schema field
descriptions hold 86 more upper-case keyword sentences, which aren't
included.

### By release

| Version      | Shopping rules | Testable MUST stated | Pinned testable MUST first stated |
| ------------ | -------------: | -------------------: | --------------------------------: |
| `2026-01-11` |            140 |                   22 |                                16 |
| `2026-01-23` |            150 |                   23 |                                 1 |
| `2026-04-08` |            379 |                   64 |                                44 |
| `2026-08-25` |            512 |                  102 |                                41 |
| draft        |            507 |                  104 |                                 2 |

- **Removed rules:** 22, of which 8 are testable MUST rules that the review
  hasn't looked at yet. Among them are the webhook rules of the first two
  releases: payloads signed as a detached JWT in a `Request-Signature`
  header, which 2026-04-08 replaced with RFC 9421 signatures.
- **Moved out:** 72 statements in older releases: 53 now on
  `embedded-protocol.md`, and 19 header rules the shopping pages now render
  with a macro (see [Release history](#release-history)).

## Regenerate

From the repository root, with a `ucp` checkout at `f2626c28`, a `ucp` clone
that has the commits in `releases.json` (the `release/*` branches of the
`Universal-Commerce-Protocol/ucp` repository), and a `.github` checkout at
`1687b56`:

```bash
uv run --no-project requirements/tools/build_catalog.py \
  --spec-root=<ucp checkout> \
  --spec-commit=f2626c28b8288976eee5bda77f3a9b1ddc0132b1 \
  --releases-repo=<ucp clone> \
  --extractor-dir=<.github checkout>/org-tools/requirements-extractor/scripts
```

The output is deterministic: a second run writes identical bytes. Add
`--check` to write nothing and fail if any output, the lock included, would
change.

Keep `catalog/requirements.lock.json` committed with the catalog. It is the
only record of the IDs issued so far: without it, the next run would issue
every ID again, from scratch.

The `catalog/` folder is excluded from prettier and codespell in
`.pre-commit-config.yaml` so that it stays exactly as the tool writes it.

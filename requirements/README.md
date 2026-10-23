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
| `catalog/extraction_report.json` | The extractor's diagnostics for the run                                               |
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
   number is never issued again.

A rule that moves to another page is retired on the old page and gets a new
ID on the new one. If one of two identical sentences on a page is removed, the
copy that stays keeps the older ID, whichever copy was removed.

These IDs are stable within this demo only. Official IDs will be issued in
the `ucp` repository, in a different order. Every lock entry records its
`text_digest`, so demo IDs can be mapped to official ones.

## Linking tests to rules

Each shopping test says which rules it checks, with one marker from
`framework/decorators.py`:

- `@spec_assert("REQ-SHOPPING-CHECKOUT-0133", ...)`: the rules its assertions
  check, by stable ID, each with a comment giving the rule's wording;
- `@no_rule("...")`: the spec describes the behaviour without an RFC 2119
  keyword, so there is no rule to cite;
- `@scenario("...")`: the test checks a store's own data, such as a
  free-shipping threshold, rather than a rule;
- `@outside_catalog("...")`: the test checks a rule on a specification page
  this catalog doesn't cover, such as the core overview page;
- `@older_version("2026-04-08", "...")`: the test checks behaviour from an
  older release that this specification has changed, so it needs updating.

`framework/requirements.py` looks rules up in the catalog. Before any test
runs, the runner checks every shopping test's marker against it, and so does
pytest:

- **Problems** stop the run: a test with no marker, or an ID that isn't in the
  catalog, was retired, or the review found isn't a rule.
- **Warnings** don't: a rule this suite can't test, or one outside the
  capabilities the test requires.

`--dry-run` runs the check without a server. `--requirements_check=warn`
(`--requirements-check=warn` with pytest) reports problems without stopping,
and `off` skips the check. Unit tests for the markers, the loader and the
check are in `tests/`: run `python -m pytest tests`.

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

## Regenerate

From the repository root, with a `ucp` checkout at `f2626c28` and a `.github`
checkout at `1687b56`:

```bash
uv run --no-project requirements/tools/build_catalog.py \
  --spec-root=<ucp checkout> \
  --spec-commit=f2626c28b8288976eee5bda77f3a9b1ddc0132b1 \
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

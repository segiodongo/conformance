# Shopping requirements catalog (demo)

> **Demo only, not for merge.** This catalog is generated from a pinned copy of
> the specification with two options the requirements extractor doesn't have
> yet, and its IDs are provisional: they change whenever a rule is added above
> them on the same page.

## Contents

| Path                             | What                                                                                  |
| -------------------------------- | ------------------------------------------------------------------------------------- |
| `catalog/shopping/**/*.json`     | One file per specification page, with every rule on that page                         |
| `catalog/capabilities.json`      | Each capability: extension or not, what it extends, its schema, pages and rule counts |
| `catalog/extraction_report.json` | The extractor's diagnostics for the run                                               |
| `review.json`                    | A person's decisions on 199 rules the automatic pass couldn't settle                  |
| `tools/build_catalog.py`         | Regenerates the `catalog/` folder from the inputs below                               |

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
- **Review:** decisions are keyed by page and `text_digest`, so they survive
  ID renumbering. A decision whose wording no longer matches a rule fails the
  build.

Each record keeps the extractor's fields; everything the demo adds is in its
`demo` object.

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

The output is deterministic: a second run writes identical bytes. The
`catalog/` folder is excluded from prettier and codespell in
`.pre-commit-config.yaml` so that it stays exactly as the tool writes it.

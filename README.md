# neuprint-neo4j-diagnostics

Read-only diagnostics for the Neo4j databases behind neuPrint.

These were written while upgrading [`flyem-snapshot`][fs]'s Neo4j dependency
from 4.4.16 to the CalVer line ([PR #71][pr]), and they are kept out of that
repo on purpose: they probe **live production servers** rather than a built
snapshot, so they do not belong in the ingestion pipeline's package. Nothing
here writes to a database.

Each one exists because a question came up that `SHOW INDEXES` and the usual
health metrics could not answer.

[fs]: https://github.com/janelia-flyem/flyem-snapshot
[pr]: https://github.com/janelia-flyem/flyem-snapshot/pull/71

## The scripts

### `audit-inert-indexes.sh` — find indexes that cover nothing (Bolt)

An index over a label no node carries reports `state: ONLINE` and
`populationPercent: 100.0`, because an index over nothing is *trivially* fully
populated. Both values are accurate and both are useless. The only way to
detect one is to ask whether any node actually carries the label.

Two cheap passes: one `SHOW INDEXES`, then one count-store lookup per distinct
label (1 DbHit each, so speed is independent of database size). Works on 4.4
and 5.x/CalVer alike — no APOC, no version-specific syntax.

```bash
./audit-inert-indexes.sh                                    # local, default db
CYPHER_SHELL="docker exec -i neo4j-wasp cypher-shell" ./audit-inert-indexes.sh
NEO4J_DB=data ./audit-inert-indexes.sh
```

### `audit-inert-indexes-http.sh` — the same, over HTTPS

For when you have neither Bolt nor container access. Two queries per dataset
via the neuPrint API. Given a server but no dataset, it audits every dataset on
that server in sequence.

```bash
export NEUPRINT_TOKEN=<your token>
./audit-inert-indexes-http.sh https://neuprint-fish2.janelia.org fish2
./audit-inert-indexes-http.sh https://neuprint.janelia.org      # all datasets
```

Prefer the Bolt version if you can reach the host; this one exists because
production hosts are not always reachable that way.

### `demo-inert-index.sh` — show what an inert index costs

Reproduces the defect the audits were written to find, in a throwaway
container, and profiles the difference. Creates 190,774 nodes (fish2's
non-synaptic element count), builds the *broken* index, profiles the query a
client would really run, then adds the correct index and profiles it again.

Measured ~2000x more reads with the broken index — `381,549` DbHits against
`191` — identically on 4.4.16 and 2026.08.1. Results are correct either way;
what you lose is performance, and what you get is a clean bill of health that
means nothing.

```bash
./demo-inert-index.sh                                        # needs podman
CLI=docker ./demo-inert-index.sh
IMAGE=docker.io/library/neo4j:4.4.16 ./demo-inert-index.sh   # the production version
```

Takes about two minutes.

### `compare-fastquery-forms.sh` — verify a query rewrite, and survey the fleet

neuPrintExplorer's `buildFastQuery` does not compile on Neo4j 5 or later: it
mixes a non-grouping variable into an aggregating `WITH`, which Cypher 4.4
accepted and Cypher 5 rejects (`42I18`). The fix is one clause, but "it
compiles" is a much weaker claim than "it returns the same rows". This runs
both forms against real data and compares full result sets — content *and* row
order.

Datasets are enumerated from `/api/dbmeta/datasets`, so it covers whatever is
actually deployed rather than a hand-maintained list.

```bash
export NEUPRINT_TOKEN=<your token>
LIST_ONLY=1 ./compare-fastquery-forms.sh   # plan + per-dataset version survey
./compare-fastquery-forms.sh               # every dataset on all four servers
./compare-fastquery-forms.sh https://neuprint-fish2.janelia.org fish2
TERMS=lc ./compare-fastquery-forms.sh      # one search term: much quicker
```

`LIST_ONLY=1` is worth running on its own. It reports each dataset's Neo4j
version and whether the fulltext index the fast query needs actually exists,
which is what determines whether that query can run there at all.

## What these found

- **217 inert indexes on the live fish2 database**, 30% of its node indexes,
  from a colon in an element label (`fish2_:Soma` where nodes carry
  `fish2_Soma`). Identical behaviour on 4.4.16 and 2026.08.1, so a
  pre-existing defect rather than an upgrade regression.
- **The proposed `buildFastQuery` fix is equivalent**, not merely compilable:
  24 comparisons across every deployed dataset that can run the query, up to
  67,449 rows, all identical in content and row order.
- **The fast query cannot run on 10 of 16 production datasets** — 3 on Neo4j
  3.5.3 (no `CALL {}` subqueries) and 7 on 4.4.16 with no fulltext index of any
  name. Both fail with an error rather than a degraded result, so
  `useFastQuery` needs a per-dataset capability check.

The write-ups live in `flyem-snapshot`: [`inert-index-demo.md`][d] and
[`neuprint-search-query-fixes.md`][q]. Both are on the `neo4j-5-upgrade`
branch, unmerged as of this writing — see [PR #71][pr].

[d]: https://github.com/janelia-flyem/flyem-snapshot/blob/neo4j-5-upgrade/inert-index-demo.md
[q]: https://github.com/janelia-flyem/flyem-snapshot/blob/neo4j-5-upgrade/neuprint-search-query-fixes.md

## Requirements

`bash`, `python3` (stdlib only), and `curl` for the HTTP scripts. The Bolt
audit needs `cypher-shell`; the demo needs `podman` or `docker`. The HTTP
scripts need `NEUPRINT_TOKEN` and exit with a warning if it is unset.

Tested on macOS `bash` 3.2 and on Linux, so no bash-4 syntax.

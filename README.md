# neuprint-neo4j-diagnostics

Read-only diagnostics for the Neo4j databases behind neuPrint, and for the
clients that query them.

These were written while upgrading [`flyem-snapshot`][fs]'s Neo4j dependency
from 4.4.16 to the CalVer line ([PR #71][pr]), and they are kept out of that
repo on purpose: they probe **live servers** rather than a built snapshot, so
they do not belong in the ingestion pipeline's package. Nothing here writes to
a database.

Each one exists because a question came up that `SHOW INDEXES` and the usual
health metrics could not answer.

[fs]: https://github.com/janelia-flyem/flyem-snapshot
[pr]: https://github.com/janelia-flyem/flyem-snapshot/pull/71

## Where to look

The directories follow the order you would actually use them in, from a freshly
built snapshot through to a server users are hitting.

| Stage | What you are asking | Where |
|---|---|---|
| 1. Snapshot | Is the built database sound before anyone serves it? | **`flyem-snapshot`** — see below |
| 2. Ingestion | Did the import account for every node and edge? | **`flyem-snapshot`** — see below |
| 3. Live, one instance | Is this running server healthy and correct? | [`live/`](live/) |
| 4. Live, two instances | Are these two servers equivalent? | [`compare/`](compare/) |
| 5. Clients | Does neuPrintExplorer work against them? Does neuprint-python? | [`client/`](client/) |
| — | Did a specific fix land, and does it stay landed? | [`regression/`](regression/) |
| — | Why is neuron search returning too few rows? | [`findneurons/`](findneurons/) |
| — | Shared palette and report plumbing, not a tool | [`common/`](common/) |

### Stages 1 and 2 live in `flyem-snapshot`, deliberately

```bash
pixi run check-neuprint-snapshot <snapshot>/neo4j     # the gate: exit 0 or 1
pixi run inspect-neuprint-snapshot <snapshot>/neo4j   # open a shell on it
```

Those are installed entry points of the ingestion package, invoked by the
Jenkins job scripts, and they need the snapshot **directory** plus a container
to run it in — not a URL and a token. They ship with the thing they check, and
copying them here would leave two versions to drift apart. The dividing line
for this repo is therefore not the pipeline stage but what a tool points at: a
built artifact on disk, or a live server over the network.

## `live/` — one running server

### `audit-inert-indexes.sh` — find indexes that cover nothing (Bolt)

An index over a label no node carries reports `state: ONLINE` and
`populationPercent: 100.0`, because an index over nothing is *trivially* fully
populated. Both values are accurate and both are useless. The only way to
detect one is to ask whether any node actually carries the label.

Two cheap passes: one `SHOW INDEXES`, then one count-store lookup per distinct
label (1 DbHit each, so speed is independent of database size). Works on 4.4
and 5.x/CalVer alike — no APOC, no version-specific syntax.

```bash
./live/audit-inert-indexes.sh                               # local, default db
CYPHER_SHELL="docker exec -i neo4j-wasp cypher-shell" ./live/audit-inert-indexes.sh
NEO4J_DB=data ./live/audit-inert-indexes.sh
```

### `audit-inert-indexes-http.sh` — the same, over HTTPS

For when you have neither Bolt nor container access. Two queries per dataset
via the neuPrint API. Given a server but no dataset, it audits every dataset on
that server in sequence.

```bash
export NEUPRINT_JWT=<your token>
./live/audit-inert-indexes-http.sh https://neuprint-fish2.janelia.org fish2
./live/audit-inert-indexes-http.sh https://neuprint.janelia.org    # all datasets
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
./live/demo-inert-index.sh                                   # needs podman
CLI=docker ./live/demo-inert-index.sh
IMAGE=docker.io/library/neo4j:4.4.16 ./live/demo-inert-index.sh
```

Takes about two minutes.

## `compare/` — two instances, side by side

### `compare-datasets.py` — are these two equivalent?

Written for a parallel release, where the same dataset is served by an old and
a new stack at once and the question is whether anything a user can reach has
changed. Takes two `server[:dataset]` targets, so it compares either two
servers or two datasets on one server.

Five layers: `:Meta` provenance and node counts; a **connectivity sample** that
compares edges and weights for bodies present on both, because matching counts
prove nothing about which bodies are joined to which; the index inventory;
server configuration; and up to **19 read-only endpoint responses**.

```bash
./compare/compare-datasets.py neuprint-pre:yakuba neuprint-test   # two servers
./compare/compare-datasets.py neuprint-pre wasp3:v0.8 wasp3:v0.0  # one server
./compare/compare-datasets.py neuprint-pre:yakuba neuprint-test --no-timing
```

The endpoint table carries a **`cl` column** saying which client drives each
endpoint, so a difference can be traced to the thing that will actually break:

```
endpoint                       cl           A          B  verdict
dbmeta/datasets                EP      57297B     57297B  identical
cached/roiconnectivity         E          32B        32B  identical, but EMPTY on both
skeletons/skeleton             EP       4227B        ERR  ERROR
```

`E` is neuPrintExplorer only, `P` is neuprint-python only, `EP` is both. The
tags come from the clients' actual call sites. The split is lopsided — 16 of
the 19 are `E`, 3 are `EP`, none are `P` — because `cached/*` and
`npexplorer/*` simply do not exist for neuprint-python. So a clean run here
says much more about the website than about the Python client; for that, see
`client/check-neuprint-python.py`.

Differences and warnings are colorized and collected into one findings block at
the end, so a long report cannot hide one. Three things it is careful about:

- **Agreement is not correctness.** Two instances can return the same empty
  answer because both are broken — that is exactly what the `overviewRois`
  defect did to `/api/cached/roiconnectivity`. Empty-on-both is flagged.
- **Identical failures are not differences.** An endpoint that 404s on both
  sides means it is unconfigured, not that the two disagree.
- **Internal ids are excluded.** `id(n)` is assigned from import order, so two
  separately built databases always disagree on `m_id`/`n_id`/`pre_id`.

Timing is reported — A/B interleaved, first repetition discarded, median not
mean — but never changes the verdict. A timing comparison between two machines
is a measurement, not a verdict.

### `cypher-version-diff.py` — does the Cypher language version matter?

A CalVer server defaults `db.query.default_language` to `CYPHER_25`, while the
ingestion pipeline pins `CYPHER_5`. This runs the client's real queries under
both and compares row sets, rather than reasoning from a changelog.

```bash
./compare/cypher-version-diff.py neuprint-pre wasp3:v0.0
```

## `client/` — the two clients, against a real server

### neuPrintExplorer

`check-rollout.py` verifies a neuPrintExplorer release across every dataset on
a server, in two parts: whether the new bundle actually arrived, and whether
the server answers the queries that build sends. Its verdicts distinguish
"not deployed" from "deployed and broken", which matters during a rollout.

`refresh-queries.sh` re-extracts those queries from a neuPrintExplorer checkout
at a pinned ref, so what gets tested is what the component really sends rather
than a transcription of it.

```bash
export NEUPRINT_JWT=<your token>
./client/refresh-queries.sh ~/path/to/neuPrintExplorer v1.72.3
./client/check-rollout.py neuprint-pre
```

Full documentation: [`client/README.md`](client/README.md).

### neuprint-python

`check-neuprint-python.py` asks the **real `NeuronCriteria`** to generate
Cypher for twelve representative criteria shapes, scans the generated text,
then executes each as a `WHERE` clause against every dataset you name.

It exists because of a gap that let a serious defect sit in a release.
`regression/verify-isnull-compat.py` checks that `x.y IS NULL` means the same
as `NOT exists(x.y)` — but its expressions are transcribed by hand, so it
would keep passing if `NeuronCriteria` changed underneath it. This one tests
what the installed package actually emits.

```bash
# needs `neuprint` importable -- it exercises the real code, not a copy
pixi run -e test python client/check-neuprint-python.py neuprint-pre
PYTHONPATH=~/path/to/neuprint-python ./client/check-neuprint-python.py neuprint-pre vnc wasp3:v0.8
```

Two independent checks per shape:

- **The generated text.** Any `exists(` is flagged outright, since Neo4j 5
  removed it — *and this needs no migrated dataset to detect*. Pointed at a
  fleet that is still entirely 4.4, it would have caught the defect months
  before the first cutover exposed it.
- **Execution.** Each expression runs and its rows are counted. A
  `SyntaxError` on one Neo4j generation and not another is the signature of a
  removed construct.

Verified in both directions, which is the only way to know a regression test
works: against neuprint-python 0.6.4 it reports six of the twelve shapes
broken — every one that touches null handling, including `cropped=False` and
`status=['Traced', None]` — and against the fixed branch it reports `OK`.

Without `neuprint` importable it exits **3** and says so, rather than quietly
testing nothing. Note neuprint-python's own test suite reads
`NEUPRINT_APPLICATION_CREDENTIALS`; this script takes the repo's
`NEUPRINT_JWT` and passes it to `Client()` explicitly, so one token works
everywhere.

## `regression/` — did a specific fix land?

Each of these pins down one defect, and each is worth keeping because the same
mistake can come back.

- **`verify-cached-fix.py`** — the three Neo4j 5 Cypher defects in
  neuPrintHTTP's `api/cached/cached.go` ([PR #96][http96]). Runs the old and
  new form of each query on three Neo4j generations at once and expects a
  specific pattern: identical on 3.5/4.4, fixed on 2026.x.
- **`verify-isnull-compat.py`** — the `exists(x.y)` substitutions in
  neuprint-python's `NeuronCriteria`. Same idea, same three generations.
- **`compare-fastquery-forms.sh`** — neuPrintExplorer's `buildFastQuery`, which
  mixes a non-grouping variable into an aggregating `WITH`: accepted by Cypher
  4.4, rejected by Cypher 5 (`42I18`). "It compiles" is a much weaker claim
  than "it returns the same rows", so this compares full result sets, content
  and row order, against real data. `LIST_ONLY=1` is worth running on its own —
  it reports each dataset's Neo4j version and whether the fulltext index the
  fast query needs exists at all.

```bash
export NEUPRINT_JWT=<your token>
./regression/verify-cached-fix.py neuprint-pre
./regression/verify-isnull-compat.py neuprint-pre
LIST_ONLY=1 ./regression/compare-fastquery-forms.sh
```

[http96]: https://github.com/connectome-neuprint/neuPrintHTTP/pull/96

## `findneurons/` — search-quality diagnostics

A second set, for neuPrintExplorer's neuron autocomplete rather than for index
health. The autocomplete picks between a fulltext-index query and a label scan
per dataset, and the two agree only while the index covers every property that
dataset actually populates — which is deliberately fewer than the eleven the
search ranks on. When a populated property falls outside the index, results go
missing silently.

These measure that: index coverage per dataset, rows a user actually receives
versus the complete set, and the remediation DDL. See
[`findneurons/README.md`](findneurons/README.md).

## What these found

- **217 inert indexes on the live fish2 database**, 30% of its node indexes,
  from a colon in an element label (`fish2_:Soma` where nodes carry
  `fish2_Soma`). Identical behavior on 4.4.16 and 2026.08.1, so a pre-existing
  defect rather than an upgrade regression.
- **The proposed `buildFastQuery` fix is equivalent**, not merely compilable:
  24 comparisons across every deployed dataset that can run the query, up to
  67,449 rows, all identical in content and row order.
- **The fast query cannot run on 10 of 16 production datasets** — 3 on Neo4j
  3.5.3 (no `CALL {}` subqueries) and 7 on 4.4.16 with no fulltext index of any
  name. Both fail with an error rather than a degraded result, so
  `useFastQuery` needs a per-dataset capability check.
- **Three Neo4j 5 defects in neuPrintHTTP's cached API** ([PR #96][http96]),
  which blank three neuPrintExplorer home-screen panels on a CalVer server. One
  fails loudly with a 400; two fail silently, because `CASE x WHEN NULL`
  compares with equality and `NULL = NULL` is null rather than true. That
  pattern matched on 4.4 and does not on 5+, so an upgrade silently stops a
  fallback from running, with nothing logged.
- **The same removed syntax in neuprint-python**, where `NeuronCriteria`
  generated `exists(x.y)` in ten places — including for `cropped=False` and any
  value list containing `None`, both everyday idioms. Ordinary analysis scripts
  returned HTTP 400 against any CalVer dataset.

The write-ups live in `flyem-snapshot`: [`inert-index-demo.md`][d] and
[`neuprint-search-query-fixes.md`][q]. Both are on the `neo4j-5-upgrade`
branch, unmerged as of this writing — see [PR #71][pr].

[d]: https://github.com/janelia-flyem/flyem-snapshot/blob/neo4j-5-upgrade/inert-index-demo.md
[q]: https://github.com/janelia-flyem/flyem-snapshot/blob/neo4j-5-upgrade/neuprint-search-query-fixes.md

## Color

These reports get long. Color is what lets you scan one instead of reading it,
so every tool here — Python and shell alike — draws from one palette in
`common/style.py` and `common/colors.sh`. The meanings are fixed, so that
learning them once is enough:

| Color | Means | Looks like |
|---|---|---|
| **red** | a difference, a failure, something wrong | `DIFF`, `FAIL`, `DIFFERS`, `MISSING`, `INERT 217`, `SyntaxError` |
| **yellow** | a warning, or agreement that is not reassurance | `WARN`, `SKIPPED`, `same rows, DIFFERENT ORDER`, `identical, but EMPTY on both` |
| **green** | clean, identical, as expected | `PASS`, `identical`, `ok`, `11/11 (complete)`, `RESULT: OK` |
| **cyan** | a heading | `=== wasp3:v0.8`, section rules |
| **dim** | an aside — context, provenance, "expected to differ" | `SKIP`, `....`, `[cached]`, `(list order is arbitrary here)` |

So a run you can ignore is mostly green, and anything needing attention is red
or yellow. In a report like this, your eye goes to the two flagged rows without
reading the other seventeen:

```
  endpoint                       cl           A          B  verdict
  dbmeta/datasets                EP      57297B     57297B  identical            <- green
  cached/roiconnectivity         E          32B        32B  identical, but EMPTY on both   <- yellow
  npexplorer/completeness        E        3927B      3927B  identical            <- green
  skeletons/skeleton             EP       4227B        ERR  ERROR                <- red

  --- findings (1 difference(s), 9 warning(s)) ---
    DIFF  endpoint / skeletons/skeleton: failed on wasp3:v0.0 only   <- DIFF in red
    WARN  endpoint / cached/roiconnectivity: both sides returned an empty response
```

### Turning it on and off

Three values, the same three for both controls:

| Value | What it does | When you want it |
|---|---|---|
| **`auto`** | **The default.** Color on when stdout is a terminal, off when it is piped or redirected. | Almost always. It keeps escape codes out of files without your having to think about it. |
| **`always`** | Force color on, even through a pipe. | When the thing reading the pipe *can* render color — `\| less -R`, `script`, capturing a screenshot — which is the one case auto gets wrong. |
| **`never`** | Force color off, even on a terminal. | A terminal that mangles ANSI, or a log you want clean. Largely redundant with `NO_COLOR`, which does the same across every tool at once. |

Two controls, meaning the same three things:

```bash
./compare/compare-datasets.py A B --color always    # Python tools take a flag
COLOR=always ./live/audit-inert-indexes-http.sh ... # shell scripts take an env var
COLOR=always ./compare/compare-datasets.py A B      # ...and so do the Python ones
```

The shell scripts take positional arguments and cannot spare a `--color` flag,
hence `COLOR`. The Python tools honor **both**, so one `COLOR=always` covers
everything and you need not remember which tool is which.

Precedence, highest first:

1. an explicit `--color` on the command line, `--color auto` included
2. the `COLOR` environment variable
3. `NO_COLOR` — presence alone disables, per [no-color.org](https://no-color.org)
4. whether stdout is a terminal

Default behavior is therefore: **on when you are reading it, off when a machine
is.** The case worth knowing is a pager, where auto-detection is wrong because
the pipe can in fact render color:

```bash
COLOR=always ./client/check-rollout.py neuprint-pre | less -R
```

An unrecognized `COLOR` value is ignored rather than fatal — it is ambient
environment, often set years ago for some other tool, and failing a diagnostic
run over it would be obnoxious. An unrecognized `--color` value *is* rejected,
because you typed that one.

### Two guarantees

**Saved reports are always plain text.** `results/*.txt` has escape sequences
stripped before writing, so a report stays readable years later and pastes
cleanly into a ticket.

**Generated code is never colored.** `findneurons/gen-index-fix.sh` emits
Cypher DDL meant to be piped into `cypher-shell`, and it does not source the
palette at all. `COLOR=auto` would protect the piped case, but someone running
it on a terminal and copying the output would carry invisible codes into their
statements. The rule is: color reports, never generated code.

## One warning that applies to everything here

**`/api/dbmeta/datasets` returns fewer datasets to an anonymous client than to
a token holder.** On `neuprint.janelia.org` that is nine versus ten, and the
hidden one was the worst case we found. An unauthenticated audit can produce a
confident all-clear that means nothing.

## Requirements

`bash`, `python3` (stdlib only), and `curl` for the HTTP scripts. The Bolt
audit needs `cypher-shell`; the demo needs `podman` or `docker`; the
`findneurons` extractor needs `node` and a neuPrintExplorer checkout.

**One deliberate exception to "stdlib only":**
`client/check-neuprint-python.py` needs `neuprint` importable, because its
whole purpose is to exercise that package's real query builder rather than a
transcription of it. Run it under an environment that has it (`pixi run -e
test python ...`, or `PYTHONPATH=<checkout>`); without it the script exits 3
and says so, rather than reporting a pass it never earned.

**Every HTTP script here reads `NEUPRINT_JWT`, falling back to
`NEUPRINT_TOKEN`**, and uses it only in a request header — never printed, never
written to a report, never placed on a command line. One token authenticates
against all the Janelia servers, since they share an auth backend.
`compare-datasets.py` additionally accepts a per-server override,
`NEUPRINT_JWT_<SERVER>`, for the case where that stops being true.

Note that **neuprint-python uses a different variable**,
`NEUPRINT_APPLICATION_CREDENTIALS`, and its test suite raises at import time if
that one is unset. Nothing in this repo reads it, but it is the same token:

```bash
export NEUPRINT_APPLICATION_CREDENTIALS="$NEUPRINT_JWT"
```

Tested on macOS `bash` 3.2 and on Linux, so no bash-4 syntax.

`results/` holds timestamped output from the Python tools and is gitignored —
it is evidence, not source, and this repo is public while those reports name
internal hosts and carry dataset statistics.

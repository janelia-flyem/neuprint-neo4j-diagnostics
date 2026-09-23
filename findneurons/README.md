# FindNeurons search diagnostics

Tools for auditing the neuron autocomplete in neuPrintExplorer's
`NeuronInputField`, which chooses between two Cypher queries per dataset:

- **`buildSlowQuery`** — scans the `:Neuron` label with `CONTAINS` against
  eleven properties. Works anywhere, always complete.
- **`buildFastQuery`** — finds candidates through the
  `find_neurons_fulltext_properties_index` fulltext index, then ranks them.

The fast one is only equivalent if the index covers all eleven properties. If
it covers fewer, a neuron whose only match is in an uncovered property is
never retrieved, and the search silently returns fewer rows — no error
anywhere. These scripts measure that.

Everything here is **read-only** except `gen-index-fix.sh`, which only prints
statements for you to review.

## Start here

```bash
# 1. Pull the queries out of the client you care about
./extract-queries.sh ~/src/neuPrintExplorer origin/master ./queries

# 2. Which datasets have a complete index?
NEUPRINT_JWT=... ./index-coverage.sh https://neuprint.janelia.org

# 3. What do users actually receive versus the complete result set?
NEUPRINT_JWT=... ./served-vs-complete.sh https://neuprint.janelia.org a ./queries
```

## AUDIT AUTHENTICATED

`/api/dbmeta/datasets` returns a **different set of datasets depending on the
token**. On `neuprint.janelia.org` an anonymous client sees nine datasets and
a token holder sees ten. The hidden one, `banc:v888`, turned out to be the
worst case in the whole fleet — 66.6% of results lost.

An unauthenticated audit of that server produced a confident all-clear. A
clean result without a token means nothing. `served-vs-complete.sh` prints a
warning when it runs without one.

Tokens are read from `NEUPRINT_JWT` or `NEUPRINT_TOKEN` and used only in a
request header — never printed, written to a file, or placed on a command
line. All output here is safe to paste into a ticket.

## The scripts

### `extract-queries.sh <repo> [git-ref] [outdir]`

Writes `fast.cypher`, `slow.cypher`, `version.cypher` and `index.cypher` from
`NeuronInputField.jsx` at a given git ref. The search queries keep `TERM` and
`BODY` placeholders for callers to substitute.

Everything else reads these files, so what gets tested is what the component
actually sends rather than a transcription. The git ref matters because the
capability check has changed twice — extract at two refs to compare the
deployed client against a proposed one.

### `index-coverage.sh <server>`

Per dataset: the index's state, how many of the eleven properties it covers,
and which are missing. This is the fastest way to see whether a server is
exposed at all.

### `served-vs-complete.sh <server> [term] [queries-dir]`

The important one. Replays the client's capability decision, runs whichever
query that implies, and compares it against `slow.cypher` as ground truth.
The `LOST` column is what a user fails to see.

It infers the decision logic from the extracted `index.cypher`, so it follows
whichever client you extracted:

| `index.cypher` contains | decision style |
|---|---|
| `db.indexes()` | name existence only (pre-#383) |
| `state, properties` | state and full coverage (the fix) |
| otherwise | state only (#383 as merged) |

Which makes it straightforward to compare two client versions against the
same server. Extracting `origin/master` and the fix branch in turn, then
running both against `neuprint-test`:

```
# origin/master        -> capability check: state-only
flywire-fafb:v783b  fast   4391 served   4397 complete   6 lost
# the coverage fix     -> capability check: coverage
flywire-fafb:v783b  slow   4397 served   4397 complete   0 lost
```

Same server, same term, different client: the fix moves that dataset off the
fast path and the loss goes to zero.

### `term-loss.sh <server> <dataset> [terms] [queries-dir]`

Fast versus slow for one dataset across several terms. Use it to see how the
loss depends on the term — a specific term may lose nothing while a broad one
loses most of the result set, which is why this hides so well.

### `gen-index-fix.sh <server> <dataset>`

Prints the `DROP INDEX` / `CREATE FULLTEXT INDEX` needed to rebuild a
dataset's index with all eleven properties, reading the live index for its
label so the statements are right rather than guessed. **Prints only.**

Rebuilding is the remedy that makes search correct *and* fast. Note the index
repopulates in the background and reports `POPULATING` until it finishes.

### `find-demo-case.sh <server> <dataset>`

Finds a browser-observable case: a neuron whose `class` value appears in no
`type`, `instance` or `synonyms`, so only a class search can find it. Type
that value into the web UI and it should be offered; on an incomplete index
it is not.

## Findings, 2026-09-23

All four production servers, enumerated with a token:

| server | dataset | index | lost (term `a`) |
|---|---|---|---|
| `neuprint` | `banc:v888` | 3/11 | **66.6%** |
| `neuprint` | `male-cns:v1.0` | 11/11 | none |
| `neuprint-yakuba` | `yakuba-vnc` | 3/11 | **37.2%** |
| `neuprint-fish2` | `fish2`, `fish2:v0.7` | 3/11 | none |
| `neuprint-pre` | `hemibrain`, `vnc` | none | none |
| `neuprint-test` | `flywire-fafb:v783b` | 3/11 | was 58%, 0 with the fix |

Five indexed datasets, four of them incomplete, two currently losing rows.
Everything unlisted has no index or runs Neo4j 3.5, so it is already on the
slow query.

Three things worth knowing when reading numbers like these:

- **The gap sets a ceiling; annotation decides how much is realised.** fish2
  has the same 3/11 gap as yakuba and loses nothing, because its uncovered
  properties are barely populated. That makes it a dormant failure, not an
  absent one — and fish2 is under active annotation.
- **They drift.** yakuba measured 5,983/9,541 one day and 6,002/9,564 the
  next. Do not treat a figure as exact.
- **The incomplete index comes from the ingestion pipeline.** `fish2:v0.6`
  became `fish2:v0.7` within a week, built with the same 3/11 index.
  `flyem-snapshot` emits all eleven properties as of commit `644158a`, which
  at the time of writing is on its `neo4j-5-upgrade` branch and not on master
  — so rebuilt datasets keep getting the incomplete index until that lands.

## Related

The client-side fix is `fix(FindNeurons): require full index coverage` in
neuPrintExplorer, with the reasoning in that repo's
`FINDNEURONS_FAST_PATH.md`. The server-side fix is `flyem-snapshot`'s
`644158a`.

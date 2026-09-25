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
`NeuronInputField.jsx` at a given git ref, plus a copy of `lucene.py`. The
search queries keep placeholders for callers to substitute:

| placeholder | meaning |
|---|---|
| `TERM` | the raw search term, for the Cypher literals |
| `BODY` | the bodyId |
| `LUCENE` | the fulltext query string |

`LUCENE` appears only for clients from **v1.72.3** onward, which build that
string in JavaScript (`buildLuceneQuery`) instead of `'*' + q + '*'` in
Cypher. `lucene.py` mirrors that rule so the consuming scripts share one
implementation; earlier revisions have no such placeholder and the
substitution is a no-op.

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

| `index.cypher` contains | decision style | seen in |
|---|---|---|
| `db.indexes()` | name existence only | before #383 |
| `state, properties` | state and full coverage | #385 only, removed by #387 |
| otherwise | state only | #383, and current |

Which makes it straightforward to compare two client versions against the
same server. Extracting two refs in turn and running both against
`neuprint-test` showed what the short-lived coverage check did:

```
# state-only        flywire-fafb:v783b  fast   4391 served  4397 complete  6 lost
# with coverage     flywire-fafb:v783b  slow   4397 served  4397 complete  0 lost
```

The coverage check was removed in #387 -- the index is the contract, and an
index covering too little is fixed by rebuilding it rather than by the client
falling back to a label scan. The comparison remains useful for any pair of
refs; this one is kept because it is what the decision was made on.

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

## Fixes that landed

Two client changes came out of this work, both merged and released in
neuPrintExplorer **v1.72.3**, and both validated against
`neuprint-test.janelia.org` afterwards:

- **Punctuation in search terms** (#386). `R1-R6` returned 0 of 3,377
  matching neurons, because the analyzer splits on punctuation and no token
  equals `r1-r6`. Now `*r1* AND *r6*`. Re-measured after deployment:
  `R1-R6`, `KCab-s`, `SNta02,SNta09` and an apostrophe term all return
  exactly what the slow query returns.
- **Trust the index** (#387). The client no longer compares the index against
  the eleven searched properties or falls back to a label scan when it falls
  short. What the index covers is what is searchable.

Separately, **rebuilding an index fixes the loss outright**. `yakuba-vnc` was
rebuilt on 2026-09-24 and went from 3/11 coverage to 11/11; its worst term
went from 318 of 21,183 rows to 21,182 of 21,182. That is the remedy these
tools exist to point at.

Note how that was achieved, because it is not yet automatic: the eleven
properties were listed explicitly under
`find-neurons-fulltext-index-properties` in `yakuba/yakuba-master-snapshot.yaml`
in the `snapshot-configs` repository. `flyem-snapshot`'s **default is still
three** on master, so a dataset whose config does not set the list keeps
getting a three-property index however often it is rebuilt. Each dataset needs
that config edit until `644158a` reaches master.

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

The client-side fixes are in neuPrintExplorer, with the reasoning in that
repo's `FINDNEURONS_FAST_PATH.md`. Note an index-coverage check was added
there and then removed again (#385, reverted by #387) -- the index is the
contract.

The server-side fix is `flyem-snapshot`'s `644158a`, still on its
`neo4j-5-upgrade` branch. Until it lands on master, a dataset gets all eleven
properties only if its own config in `snapshot-configs` lists them.

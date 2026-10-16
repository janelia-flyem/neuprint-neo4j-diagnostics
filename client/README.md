# neuPrintExplorer rollout checks

A go/no-go check for one server at a time, as the new neuPrintExplorer
(v1.72.3+) is rolled out across the fleet.

```bash
export NEUPRINT_JWT=...            # required; see "Authenticate" below
./check-rollout.py neuprint-pre
```

Writes `results/<server>-<timestamp>.txt` (the report you just watched) and
`results/<server>-<timestamp>.tsv` (one row per dataset/term, for diffing one
machine against another).

| exit | RESULT | meaning |
|---|---|---|
| 0 | `OK` | new client live, server answers it correctly |
| 3 | `NOT DEPLOYED (server checks passed)` | server is ready; the bundle just is not live yet |
| 1 | `FAILED (server checks)` | do not roll out here until resolved |
| 2 | `INCOMPLETE` | run could not complete — no token, bad invocation, unreachable |
| 0 or 3 | `NOTHING TO VERIFY` | no dataset here reaches the fast path, so nothing was compared |

`NOTHING TO VERIFY` exists because `passed: 0` with no failures would otherwise
render as a clean bill of health. A server whose every dataset runs Neo4j below
4.4, or has no fulltext index, cannot be broken by these changes — but nothing
about its query behavior was established either, and the report should not
imply otherwise. Check the `comparisons run:` line in the summary: if it is
zero, no fast-vs-slow comparison happened.

1 and 3 are separate because **before** a rollout the deployment check is
*supposed* to fail, and that must not look like a server that mishandles the
client's queries. So the same script serves as a pre-flight readiness check
(expect 3) and as a post-deploy gate (expect 0).

A server failure outranks a deployment failure: if the server gets the
client's queries wrong, deploying there is wrong whether or not it has already
happened.

## What it checks, and why in two parts

**1. Is the new client actually deployed?** The served bundle is fetched and
grepped for the fixes:

| marker | must be | what it proves |
|---|---|---|
| `SHOW INDEXES` | present | the Neo4j 5 capability probe (#383) |
| `db.indexes()` | absent | the call Neo4j removed in 5.0 is gone (#383) |
| `[^a-z0-9_]+` | present | `buildLuceneQuery`'s tokenizer (#386) |

This part exists because everything else here probes the **server**, and the
server answers identically whether the new client is live or not. Without it a
clean run says nothing about whether the rollout arrived. Validated against
both ends: 3/3 pass on `neuprint-test` (new), 3/3 fail on `neuprint-pre` (old,
as of 2026-09-30).

It needs no credentials, so it runs before the auth gate — without a token you
still get this answer.

**2. Does the server answer the client's queries correctly?** For each dataset
the capability decision is replayed (Neo4j version, then index state), the
implied query is run, and the rows are compared against `buildSlowQuery` as
ground truth.

## Authenticate

`/api/dbmeta/datasets` returns **fewer datasets to an anonymous client** —
nine versus ten on `neuprint.janelia.org`, and the hidden one turned out to be
the worst case in the whole fleet. So an unauthenticated audit produces a
confident all-clear that means nothing, and this script refuses to treat one
as a pass.

Set `NEUPRINT_JWT` or `NEUPRINT_TOKEN`. The token is sent in a request header
only — never printed, written to the report, or placed on a command line, so
the output is safe to paste into a ticket.

## How to read the verdicts

**PASS** — the fast query returned every row the slow query did. Over-matching
is not a failure: the client re-filters each row with `.includes(inputValue)`,
so the index query only has to return a superset. Under-matching loses results
silently, which is the thing being tested.

**WARN (index gap)** — rows were lost, *and* the dataset populates searched
properties that its fulltext index does not cover, which fully explains the
loss. Not a rollout regression: since #387 the client trusts the index by
design, and an index covering less than the query searches is a config choice
in `flyem-snapshot` — the default indexes `type`, `instance` and `synonyms`,
and a dataset annotating more extends
`find-neurons-fulltext-index-properties` in its own snapshot config. Fix it
there and rebuild, not here.

**FAIL (unexplained loss)** — rows were lost with no coverage gap to account
for them. That is the signature of a client or server defect and should stop
the rollout.

**FAIL (query error)** — the query did not execute. The two we fixed look like
`42I18` (Cypher 5 implicit grouping) and `42N08` (`db.indexes()` removed in
5.0). Note neuPrintHTTP returns HTTP 400 for *every* Cypher error including a
lost Bolt connection, so read the message rather than the status.

**SKIP** — the client would use the slow query here anyway: Neo4j below 4.4
(no `CALL {}` subqueries), or no `ONLINE` fulltext index. `buildSlowQuery` is
a label scan and always complete, so there is no divergence to test.

**NO-COMPARISON** — the term matched nothing on this dataset, so there was
nothing to compare and no conclusion to draw. Deliberately *not* a PASS:
"the fast query lost no rows" is trivially true when there were none to lose,
and counting it would inflate the verdict. If no term on a dataset yields a
comparison, that becomes a WARN — the fast path was never exercised there.

## Terms

Default: `a`, `R1-R6`, `KCab-s`, `SNta02,SNta09`, `Tm5'`, `LC(L)`.

Everything after `a` is punctuation that used to return **zero** rows: the
Lucene analyzer splits on punctuation, so no token ever equals `r1-r6`, and a
wildcard query for `*r1-r6*` matched nothing. Escaping does not help; the fix
was to tokenize and join — `*r1* AND *r6*`.

Those names are hemibrain- and optic-lobe-flavored, though, and mean nothing
on (say) wasp3 — where all four returned zero rows and produced four
meaningless passes on the first run. So **the script also asks each dataset
for a punctuated value of its own** and appends a fragment of it:

```
MATCH (n:Neuron) WHERE n.type =~ '.*[^A-Za-z0-9_ ].*' RETURN n.type LIMIT 1
```

A fragment spanning the punctuation is used rather than the whole value
(`GL-nonORN(L)` → `GL-nonORN`), because an exact value matches itself and
little else, whereas the fragment is the shape that used to fail. It may look
unbalanced — `LC(L)` yields `LC(L` — which is correct: this is substring
search, and that is what the component passes through. The report prints the
fragment and the Lucene query it becomes.

That means the punctuation path gets exercised on every dataset regardless of
naming convention. Override or extend the fixed list by passing your own:

```bash
./check-rollout.py neuprint-pre 'a' 'PLP(R)' "KCg-m"
```

## Keeping the queries honest

`queries/*.cypher` are extracted from `NeuronInputField.jsx` rather than
transcribed, so what gets tested is what the component actually sends.
Refresh or re-pin them with:

```bash
./refresh-queries.sh ~/Documents/workspace/Git/neuPrintExplorer v1.72.3
```

Which ref you extract decides **whose** behavior is measured — the capability
check has changed twice (#383 added `SHOW INDEXES`; #385 added a coverage
check that #387 removed again). `refresh-queries.sh` records the repo, ref and
resolved commit in `queries/SOURCE`, and every report prints it:

```
queries: neuPrintExplorer @ origin/master (d8c043c), extracted 2026-09-30
```

Note this is independent of what the bundle check finds, and the two need not
agree. Running before a rollout gives you exactly the useful combination:
the deployed-client section FAILS (not there yet) while the query sections
PASS (it will work when it arrives). Afterwards, both should pass.

`check-rollout.py` re-implements `escapeForCypher` and `buildLuceneQuery` to
fill the `TERM`/`BODY`/`LUCENE` placeholders. Those two must stay in step with
the client; if the autocomplete's query building changes again, update them
together.

## Related

Deeper per-dataset diagnostics, for when this reports something you want to
dig into, live in
[janelia-flyem/neuprint-neo4j-diagnostics](https://github.com/janelia-flyem/neuprint-neo4j-diagnostics)
— `served-vs-complete.sh`, `term-loss.sh`, `index-coverage.sh` and
`gen-index-fix.sh`. This repo is the thin rollout gate; that one is the
microscope. The reasoning behind the client changes is in
neuPrintExplorer's `FINDNEURONS_FAST_PATH.md`.

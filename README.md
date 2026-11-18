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

## Running

Every tool runs directly (`./compare/compare-datasets.py ...`): they are
stdlib Python, bash and curl. [pixi](https://pixi.sh) adds a pinned
interpreter and a memorable name for each one, and `pixi task list` is the
menu:

```bash
pixi install                        # the default environment: python + curl, small
pixi task list                      # one task per tool, with a one-line description
pixi run fleet --no-meta            # any task; arguments pass through unchanged
pixi run compare neuprint-pre wasp3:v0.8 wasp3:v0.0
pixi run rollout neuprint-pre       # umbrella: the three checks after a neuPrintHTTP deploy
```

Task names are the verb and the thing: `fleet`, `compare`, `cypher-diff`,
`audit-indexes`, `audit-indexes-bolt`, `demo-inert-index`, `check-explorer`,
`check-python`, `check-r`, `refresh-queries`, `verify-cached`,
`verify-isnull`, `compare-fastquery`, `index-coverage`, `served-vs-complete`,
`term-loss`, `find-demo-case`, `gen-index-fix`, `extract-queries`. Each
section below shows the task next to the direct invocation.

Two more environments exist, installed only when asked for because of their
size. `pixi install -e clients` adds neuprint-python and node, for
`check-python` and `extract-queries`; pixi picks that environment by itself
when you run those tasks. `pixi install -e r && pixi run setup-r` builds the
R toolchain for `check-r` (see [Requirements](#requirements)). The two
umbrella tasks, `rollout SERVER` and `clients SERVER "DATASET ..."`, take
exactly the arguments shown; put `--` before anything you want passed through
to the tools underneath. Like `&&`, an umbrella stops at the first check that
fails, so a FAILED result names the first failing tool, not necessarily the
only one.

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
| — | Which dataset is on which service, on which host, and does everything agree? | [`fleet/`](fleet/) |
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
./live/audit-inert-indexes.sh                               # local; finds the one user database
CYPHER_SHELL="docker exec -i neo4j-wasp cypher-shell" ./live/audit-inert-indexes.sh
NEO4J_DB=data ./live/audit-inert-indexes.sh                 # name it when a server has several
pixi run audit-indexes-bolt                                 # the same, under pixi
```

### `audit-inert-indexes-http.sh` — the same, over HTTPS

For when you have neither Bolt nor container access. Two queries per dataset
via the neuPrint API. Given a server but no dataset, it audits every dataset on
that server in sequence.

```bash
export NEUPRINT_JWT=<your token>
./live/audit-inert-indexes-http.sh https://neuprint-fish2.janelia.org fish2
./live/audit-inert-indexes-http.sh https://neuprint.janelia.org    # all datasets
pixi run audit-indexes https://neuprint.janelia.org                # the same, under pixi
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
./live/demo-inert-index.sh                                   # podman or docker, whichever is on PATH
CLI=docker ./live/demo-inert-index.sh                        # or choose
IMAGE=docker.io/library/neo4j:4.4.16 ./live/demo-inert-index.sh
```

Takes about two minutes.

## `compare/` — two instances, side by side

### `compare-datasets.py` — are these two equivalent?

Written for a parallel release, where the same dataset is served by an old and
a new stack at once and the question is whether anything a user can reach has
changed. Takes two `server[:dataset]` targets, so it compares either two
servers or two datasets on one server.

Six layers: `:Meta` provenance and node counts; **per-ROI synapse totals**
summed out of every Segment's `roiInfo`; a **connectivity sample** that
compares edges and weights for bodies present on both, because matching counts
prove nothing about which bodies are joined to which; the index inventory;
server configuration; and **19 read-only endpoints**, 23 responses at the
default `--samples 3`.

The `roiInfo` layer is there because counts, types and sampled connectivity can
all match exactly while every ROI figure a user sees is different — observed on
wasp3, where all 95 ROIs disagreed and the only sign of it was eight endpoints
flagging downstream. `roiInfo` is stored as a JSON string at CSV import rather
than computed per query, so a difference in it is always the pipeline that
built the CSVs, never the server. It needs APOC for the JSON parse and is
skipped with a warning where that is missing; `--no-roiinfo` turns it off, as
it is the slowest query in the data layer on a large dataset.

```bash
./compare/compare-datasets.py neuprint-pre:yakuba neuprint-test   # two servers
./compare/compare-datasets.py neuprint-pre wasp3:v0.8 wasp3:v0.0  # one server
./compare/compare-datasets.py neuprint-pre:yakuba neuprint-test --no-timing
pixi run compare neuprint-pre wasp3:v0.8 wasp3:v0.0               # the same, under pixi
```

#### Comparing a snapshot before it is copied

Either side may be a **snapshot directory** — the `conf/ data/ logs/ plugins/`
tree flyem-snapshot writes, before anyone copies it to a server:

```bash
./compare/compare-datasets.py /groups/.../wasp3-v0.0/neo4j neuprint-pre:wasp3:v1.0
```

neo4j is started on that directory in a container, queried, and stopped again.
The point is to run the comparison **before** the copy: a count discrepancy
found now costs a re-export, found afterwards it costs the transfer as well.

Four of the five layers — data, connectivity, indexes, config — only ask Cypher
questions, so they run unchanged. The endpoint, timing and path-encoding layers
need neuPrintHTTP, and nothing is serving a directory; they are skipped with a
notice rather than failing a row at a time.

Needs `apptainer`, so run it where the snapshot lives. The stock snapshot conf
asks for a 31G heap and a 150G page cache — on a smaller host, pass
`--snapshot-heap 8G --snapshot-pagecache 8G`. Ports are picked free at startup
and bound to loopback, so this never collides with a neo4j already running on
the node.

Starting the server also reads `Meta.tag`, which is what neuPrintHTTP composes
the served name from (`dataset:tag`). `check-neuprint-snapshot` does not look
at it, and an empty one is what makes a parallel release collide — so the
report names this side the way it will actually be served, and says so when
there is no tag.

The endpoint table carries a **`cl` column** saying which client drives each
endpoint, so a difference can be traced to the thing that will actually break:

```
endpoint                       cl           A          B  verdict
dbmeta/datasets                EP      57297B     57297B  identical
cached/roiconnectivity         E          32B        32B  identical, but EMPTY on both
skeletons/skeleton[11005]      EP       4227B        ERR  ERROR
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

Meshes and skeletons are checked against **`--samples` keys each** (default 3),
and named for the key they used — `roimeshes/mesh[ANm]`. Every other endpoint
runs the same query whatever input it is handed, so one input settles it. These
two read a per-key store, where one ROI and one body prove only that those are
served; yakuba v0.0 had a store wired to one dataset and silent for the other,
and a single sample is a thin way to catch that. The timing and path-encoding
layers still use one key per route, since neither question is about the key.

Timing is reported — A/B interleaved, first repetition discarded, median not
mean — but never changes the verdict. A timing comparison between two machines
is a measurement, not a verdict.

After the findings block comes an **attribution section**, which groups those
same findings by the evidence that explains them: a differing
`latestMutationId` means the segmentation moved, a differing
`lastDatabaseEdit` history means the two builds fetched annotations at
different times. It is not a confidence score — there is no labeled corpus of
drift-versus-regression to calibrate a percentage against, and a number would
invite the skimming the findings block exists to prevent. Three rules keep it
honest: it never removes a finding or changes the exit code, every group says
what it could **not** rule out, and a finding leaves `UNATTRIBUTED` only on a
signal that was actually measured, never by elimination. `UNATTRIBUTED (0)` is
printed even when empty, because that zero is the headline of a clean run.
`--no-attribution` turns the section off.

### `cypher-version-diff.py` — does the Cypher language version matter?

A CalVer server defaults `db.query.default_language` to `CYPHER_25`, while the
ingestion pipeline pins `CYPHER_5`. This runs the client's real queries under
both and compares row sets, rather than reasoning from a changelog.

```bash
./compare/cypher-version-diff.py neuprint-pre wasp3:v0.0
./compare/cypher-version-diff.py neuprint-yakuba yakuba-vnc:v0.0 "DNa02" "SNpp"   # your own terms
pixi run cypher-diff neuprint-pre wasp3:v0.0
```

The question only exists from Neo4j 5 on: pointed at a 4.4 dataset it says
"nothing to compare" and exits 2 rather than reporting twelve parse errors as
divergences. The default terms are from hemibrain and the optic lobe; a term
that matches no row under either version is reported as "not exercised", not
as agreement, and a run where every term comes up empty gets no verdict.

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
./client/refresh-queries.sh ~/path/to/neuPrintExplorer v1.72.3    # or NEUPRINTEXPLORER=~/path/to/neuPrintExplorer
./client/check-rollout.py neuprint-pre
pixi run refresh-queries ~/path/to/neuPrintExplorer v1.72.3       # the same, under pixi
pixi run check-explorer neuprint-pre
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
# needs `neuprint` importable -- it exercises the real code, not a copy.
# Under pixi the `clients` environment provides the released package:
pixi install -e clients
pixi run check-python neuprint-pre
# To test a checkout or branch instead, put it first on PYTHONPATH; it wins
# over the installed package, under pixi or not:
PYTHONPATH=~/path/to/neuprint-python pixi run check-python neuprint-pre vnc wasp3:v0.8
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

### neuprintr

`check-neuprintr.R` is the R counterpart: it calls the **real neuprintr
functions** whose Cypher changed for Neo4j 5 — `neuprint_typeof()`,
`neuprint_name_field()`, `neuprint_search()`, `neuprint_ids()`,
`neuprint_get_meta()`, `neuprint_get_synapses(roi=)`,
`neuprint_connection_table()` — against every dataset you name, and reports
per function and per dataset.

```bash
# needs R with neuprintr and its dependencies; the point is the real package
./client/check-neuprintr.R neuprint-pre wasp3:v0.0 --neuprintr-path ~/path/to/neuprintr

# the run worth keeping: one dataset per Neo4j generation on the current fleet.
# A bare dataset uses the first server named; server/dataset names its own.
./client/check-neuprintr.R neuprint hemibrain:v1.2.1 hemibrain:v1.2.3 neuprint-pre/wasp3:v0.0

# the same, from this repo's `r` environment (see "Requirements"): R, the
# dependency stack and neuprintr itself, so --neuprintr-path is only needed
# to test a checkout
pixi install -e r && pixi run setup-r            # once
pixi run check-r neuprint hemibrain:v1.2.1 hemibrain:v1.2.3 neuprint-pre/wasp3:v0.0
pixi run check-r neuprint-pre wasp3:v0.0 --neuprintr-path ~/path/to/neuprintr
```

The roll-up ends with `neo4j generations covered: 3.5.3, 4.4.16, 2026.08.1`.
A run that touched only one generation gets a **WARN**, because a construct
removed in another generation can only appear as a *difference* when both are
in the run; the emitted-text scan is the half that still works with one.

neuprintr's own test suite cannot do this job: it is pinned to hemibrain body
ids, hemibrain cell types and a saved ROI hierarchy, so against any other
dataset it fails for data reasons and says nothing about the Cypher. This
script instead **discovers** a connected body, a cell type and an innervated
ROI on each dataset, then uses those. Run across datasets on different Neo4j
generations it produces the compatibility table directly.

The same two checks per function as the Python version:

- **The emitted text.** Every statement the package sends is captured by
  tracing `neuprint_fetch_custom()` and scanned for `exists(` and
  `apoc.meta.type(` — the two things Neo4j 5 and APOC 5 removed. This needs
  no migrated dataset to detect.
- **Execution.** Each function runs with the discovered arguments and must
  return a sensible value (a `STRING`, one id, a `soma` column that is not
  `NA`). A function that runs but finds nothing is **NO-DATA**, not OK, and
  becomes a WARN in the roll-up: "did not fail" is trivially true when
  nothing was asked.

Verified in both directions, as a regression check has to be: neuprintr
1.4.0 (`e74bf69`) against a 2026.08.1 dataset reports 8 of 10 functions
broken and both constructs `EMITTED`; the fixed branch reports `OK` on 3.5.3,
4.4.16 and 2026.08.1 with both constructs `absent`.

`--neuprintr-path <checkout>` loads a checkout in place via `pkgload`, so a
branch can be tested without installing it. Without a loadable package the
script exits **3** and says so.

## `fleet/` — the whole swarm, in one picture

### `fleet-status.py` — which dataset is where, and does everything agree?

Rebuilds the "config to neo4j service map" from the live swarm instead of by
hand, and adds what a spreadsheet cannot hold: what each neuprinthttp instance
serves right now, which `dataset:tag` each running neo4j actually contains and
which public hosts expose it, when each container last started, where its
data lives and how much memory it is allowed.

```bash
export NEUPRINT_JWT=<your token>
./fleet/fleet-status.py                                    # full run
./fleet/fleet-status.py --no-meta                          # skip the cypher-shell pass
./fleet/fleet-status.py --no-endpoints                     # skip the https probes (no token needed)
./fleet/fleet-status.py --notes-from previous-map.csv      # keep the hand-written notes
./fleet/fleet-status.py --baseline fleet/baseline.txt      # accept known, intended differences
./fleet/fleet-status.py --no-report                        # terminal only, nothing under results/
./fleet/fleet-status.py --manager HOST --probe-node HOST --user NAME     # override the defaults
pixi run fleet --no-meta                                   # any of the above, under pixi
```

`--color` and `--baseline` work as in every other tool here (see `common/`).

Needs key-based ssh as the operator account (`--user`) to the swarm manager
(`--manager`), to the probe node (`--probe-node`) and to every node that runs
a neuprinthttp instance; the defaults for all three are in the source. It is read-only against the
swarm: nothing is scaled, started, stopped or written on any host. The only
thing it creates is one short-lived `docker run --rm` container on the probe
node per run, and it checks that node first. `--no-meta` skips the container
entirely and finishes in about 15 seconds. The endpoint probes run in parallel,
one thread per instance, so an unreachable endpoint costs at most one 30 s
timeout for the whole run, not one per endpoint.

It opens one ssh connection per host and reuses it for every command
(ControlMaster, sockets in a private temp dir removed at exit). Besides saving
handshakes, that makes a run survive a resolver hiccup: the swarm hosts
only resolve through the site's search domain, and once that blinked
mid-run, after the swarm and configs had been collected, leaving all
28 Meta reads of the day to fail with "Could not resolve hostname". Now the
name is looked up once, when the connection is made, and a command that dies
with ssh's own exit status is retried once. If the single probe command still
fails, the Meta pass is reported with one `probe / <node>` warning, the
header says how many stores were read, and every store is marked "not read"
rather than reported as a fault of the store.

Four sources, each collected independently so one failing does not blank the
others:

| source | from | gives |
|---|---|---|
| **swarm** | `docker service ls / inspect / ps`, `docker node ls`, `docker inspect` of the running tasks, all on the manager | replicas (Up, Down 0/0, Not deployed), image tag = Neo4j version, `host.role` placement constraint, the node(s) the task is actually on and when it started, the `/data` volume, heap and page cache settings, the swarm memory limit |
| **configs** | `/opt/conf/<instance>/conf/config.json`, read over ssh **from the node each neuprinthttp runs on** | main store (**M**) and every `mainstore-alternatives` entry (**A**), each with its `database` key if any; credentials held in memory for the Meta pass and never written |
| **endpoints** | `https://neuprint-<x>.janelia.org/api/{version,dbmeta/database,dbmeta/datasets}` | what each instance serves, from outside |
| **meta** | `MATCH (m:Meta) RETURN m.dataset, m.tag, m.uuid, m.hideDataSet, m.totalPreCount, m.totalPostCount` (as one pipe-joined string), via `cypher-shell` in one throwaway container on the probe node, over the swarm overlay network | `dataset:tag` per running store — the only way to tell `wasp3:v0.8` from `wasp3:v0.0`, whose uuid and lastDatabaseEdit are identical — plus hidden, uuid and synapse counts |

Why configs come from the running node and not the manager: every host carries
copies of every config directory, and some are stale. The first run found the
idle worker holding ten `neuprinthttp-*` directories while running no
neuprinthttp at all; those are the copies a hand edit lands in by mistake.

Why Meta goes over the overlay network: the operator account has no docker
access on one of the nodes, so `docker exec` cannot reach the stores there,
but a container on the stack's attachable overlay can reach every service by
VIP from any node. (By VIP, not by name: Java's URI parser rejects the
underscore in `neo4j-manc-v1_2_3`, and the name silently falls back to
`localhost`.)

Why the probe runs on an idle worker and not the manager, and why it is one
container rather than 28: on 2026-11-17 the manager, up for years with store
memory configured to nearly all of the box, was failing order-6 kernel
allocations, so every `docker run` risked `error creating veth pair: cannot
allocate memory`. Three Meta passes that afternoon, 28 container starts each,
hit that repeatedly, and at the end of the third the overlay sandbox on the
manager was rebuilt and every container on the node lost overlay connectivity
until the Docker daemon was restarted. Three things changed as a result:

- **The probe node is the idle worker** (`--probe-node`): far more memory than
  its one store needs, no allocation failure in its kernel log. The manager
  keeps only the read-only `docker service` queries. (Not the third node: the
  operator account cannot run docker there.)
- **One container per run.** A single `neo4j:4.4.16` container runs
  cypher-shell for every store from inside, four at a time: 4.4's
  cypher-shell speaks Bolt to the 3.5, 4.4 and 2026.08.1 servers alike
  (verified against wasp3:v0.0). A fleet of 28 stores costs one interface
  creation instead of 28, and the pass takes seconds instead of minutes.
  `--probe-image` overrides the image; note that the CalVer image's own
  cypher-shell does not run on the probe node (Docker 20.10, whose default
  seccomp profile predates the syscalls its Java needs) except with
  `--security-opt seccomp=unconfined`, which the probe deliberately avoids.
  The container's own time limit is sized from the store count (one
  `CYPHER_TIMEOUT` per round of four stores, plus a minute to start), so a
  few hung stores cannot time out the stores that did answer. If the
  container fails to start, it is retried once; a timeout is not retried.
  A store that is Up but has no VIP on the overlay is reported as not read
  rather than probed by a name that cannot resolve.
- **A pre-flight check refuses to start a container on a host that is
  failing allocations** or is short of the right kind of memory: it reads
  the node's `dmesg` for the last hour and `/proc/meminfo`, and refuses on
  any allocation failure, under 32 GB `MemAvailable`, under 16 GB `MemFree`,
  or `Committed_AS` above 1.5x `CommitLimit`. The last two are what
  `MemAvailable` alone misses: the manager that day had well over 100 GB
  available and still failed, because almost none of it was actually free
  and it was committed to several times its limit. If `dmesg` is
  unreadable, the host passes only
  with twice the headroom on every figure. On a bad answer the Meta pass is
  skipped with one `probe / <node> / unsafe` warning instead of probing.

3.5 servers have auth enabled; their credentials come from whichever
config.json points at them, travel inside the script on ssh's stdin, are
piped into the container as a bash string (no argv, no temp file), and are
handed to a shell function that sets cypher-shell's environment. At no point
is a password an argument that `ps` on any host could show.

**Verdicts.** `DIFF` is an inconsistency between two sources: a config points
at a service that is Down or does not exist; a service is Up but no config
references it; a service is Up but **nothing answers on bolt**, or bolt answers
but a one-row Meta query does not finish in two minutes, so whatever it
holds is not served; a dataset:tag is held by two running services; a
placement constraint names one node and the task runs on another; an endpoint
serves a dataset no store behind it holds, or fails to serve one that a store
does hold. `WARN` is context: an endpoint unreachable from here, a config
directory with no service, a config.json that could not be read (which also
suspends the "unreferenced" check, since a store that config might name cannot
be called unreferenced), stale config copies, a CalVer store named without a
`database` key, a probe node judged unsafe, a probe container that Docker
could not start or that did not finish in time, an Up store with no VIP on the
overlay (so it could not be probed), a Meta read that failed for a reason
other than bolt being down.

The bolt check earned its place on the first full run: three 3.5 services
(`neo4j-fib19`, `neo4j-hemibrain-v1_0_1`, `neo4j-hemibrain-v1_1_0`) reported
`1/1` replicas and had been "Up" for months, yet had no process listening on
7687 — which is why their datasets had been missing from neuprint.janelia.org
with no error anywhere. `docker service ls` cannot see that; a bolt
connection can.

**Outputs.** `results/fleet-<stamp>.txt` is the report; `results/fleet-<stamp>.csv`
is the spreadsheet with the original's columns (service, Status, version, node,
M/A per instance, notes) plus columns the original could not have. A service
that a config names but the swarm does not have still gets a row, with Status
`Not deployed`, so a retired store keeps its place (and its note) in the sheet.
Both files are gitignored: they name internal hosts.

| column | from | why |
|---|---|---|
| `Status` | `docker service ls`, plus `:Meta` | `Up`, `Down (0/0)`, `Not deployed`; and `Hidden` for an Up store whose Meta has `hideDataSet=true`, which runs and is served but is not listed by neuPrintExplorer. The judge treats Hidden as Up: hiding is a choice, not a fault. |
| `running on` | `docker service ps` | the node the task is actually on (every node, if a service has more than one task); `node` is the `host.role` placement constraint, blank when the constraint is of another kind. A mismatch is a DIFF. |
| `since` | the running task's `Status.Timestamp` | when the container last (re)started. `neo4j-fib19` had been Up for weeks while dead on bolt; this is how long. |
| `serves` | `Meta.dataset:tag` | what the store holds. Names lie: `neo4j-wasp` holds `wasp3:v0.8`, `neo4j-wasp-calver` holds `wasp3:v0.0`. |
| `served by` | each endpoint's `/api/dbmeta/datasets` | the public hosts that actually expose a dataset:tag this store holds. M/A says configured, `serves` says held, this says reachable. |
| `uuid` | `Meta.uuid` | which stores were built from the same segmentation. A uuid that is a mutable DVID head does not prove the same data (yakuba). |
| `pre / post` | `Meta.totalPreCount / totalPostCount` | size sanity: two stores claiming the same dataset:tag should agree. |
| `data dir` | the mount at `/data` | the named volume (or host path) the store lives in; needed before moving anything off a host. |
| `heap`, `page cache`, `memory limit` | `NEO4J_*_memory_heap_max__size`, `NEO4J_*_memory_pagecache_size`, the swarm resource limit | configured, not measured: what the store may use, for capacity planning when consolidating hosts. Three columns so a sheet can sum them. Only the memory keys leave the host: the same Env block can carry `NEO4J_AUTH`. |

When the probe container cannot run at all, or does not finish within its
time limit, that is reported once as `probe / <node> / <image>` with the
Docker error, and every store is marked "not read" rather than reported as a
fault of the store.

Baseline patterns suggested under the findings end in a colon, so that a
pattern for `neo4j-fish2` does not also accept `neo4j-fish2-v0_7`.

Exit 0 when consistent, 1 when any `DIFF` remains after the baseline, 2 when
the swarm could not be queried or no token is set without `--no-endpoints`.

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
pixi run verify-cached neuprint-pre                        # the same three, under pixi
pixi run verify-isnull neuprint-pre
LIST_ONLY=1 pixi run compare-fastquery
pixi run rollout neuprint-pre                              # check-explorer + both verify-* in one go
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
[`findneurons/README.md`](findneurons/README.md). Under pixi they are
`extract-queries`, `index-coverage`, `served-vs-complete`, `term-loss`,
`find-demo-case` and `gen-index-fix`.

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
  skeletons/skeleton[11005]      EP       4227B        ERR  ERROR                <- red

  --- findings (1 difference(s), 9 warning(s)) ---
    DIFF  endpoint / skeletons/skeleton[11005]: failed on wasp3:v0.0 only   <- DIFF in red
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
`pixi.toml` provides the Python, curl and node, in three environments, for
`osx-arm64` and `linux-64`; it deliberately does not provide `cypher-shell`,
`docker` or ssh keys.

| environment | adds | for |
|---|---|---|
| `default` | `python`, `curl` | everything except the two client checks and the extractor |
| `clients` | `neuprint-python` (from the `flyem-forge` channel), `nodejs` | `check-python`, `extract-queries` |
| `r` | `r-base` and neuprintr's dependency stack from conda-forge, plus compilers | `check-r`; `pixi run setup-r` finishes it |

**Two deliberate exceptions to "stdlib only":**
`client/check-neuprint-python.py` needs `neuprint` importable, because its
whole purpose is to exercise that package's real query builder rather than a
transcription of it. The `clients` environment provides the released
package, and `PYTHONPATH=<checkout>` in front of either invocation tests a
branch instead; without the package the script exits 3 and says so, rather
than reporting a pass it never earned.
`client/check-neuprintr.R` likewise needs R with neuprintr and its
dependencies (plus `pkgload` if you pass `--neuprintr-path`). It exits 3 on
the same principle. The `r` environment is 1.7 GB for one script, which is
why it is opt-in: `pixi install -e r` fetches everything conda-forge carries
as prebuilt binaries, and `pixi run setup-r` then installs the three packages
it cannot: `nat` from GitHub, because neuprintr's `DESCRIPTION` requires a
development version (`>= 1.9.2.9000`) that CRAN does not carry; `drvid`,
which is GitHub-only; and `neuprintr` itself, so that `check-r` works without
`--neuprintr-path`. If you also want to regenerate neuprintr's documentation,
add `r-roxygen2=7.3.2` to the `r` feature — the version pinned in its
`DESCRIPTION`, so a rebuild produces no spurious `.Rd` diffs.

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

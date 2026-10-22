#!/usr/bin/env python3
"""
compare-datasets.py <A> <B> [options]

Compare two neuPrint instances and report whether they are data- and
function-identical.  Written for a parallel release, where the same dataset is
served by an old and a new stack at once and the question is whether anything
a user can reach has changed.

Each of A and B is `server[:dataset]`.  Omitting B's dataset reuses A's, which
is the parallel-release case:

    ./compare-datasets.py neuprint-pre:yakuba neuprint-test:yakuba
    ./compare-datasets.py neuprint-pre:yakuba neuprint-test          # same

The original two-datasets-on-one-server form still works:

    ./compare-datasets.py neuprint-pre wasp3:v0.8 wasp3:v0.0

What it compares, in three layers:

  DATA      :Meta provenance, node counts per label, how many Neurons carry
            each searched annotation property, and type-level differences.
            This is the original comparison.

  FUNCTION  Every read-only neuPrintHTTP endpoint the client actually calls,
            invoked on both instances with identical inputs and the responses
            compared.  This is the stronger test of the two: it exercises the
            whole stack rather than Cypher in isolation, and it does not
            depend on scraping queries out of JSX that may be assembled at
            runtime.  Inputs that need a value (a cell type, a bodyId, an ROI)
            are chosen from data present on BOTH instances, so a run is
            reproducible and a difference is never just a different input.

  TIMING    The same endpoints, timed.  Calls are interleaved A,B,A,B so that
            network or load drift hits both equally, the first repetition of
            each is discarded as warm-up, and the median is reported rather
            than the mean.  Ratios outside the tolerance band are flagged but
            do NOT fail the run -- see "On timing" below.

Exit codes: 0 everything compared is identical, 1 differences found,
2 the run could not complete (bad invocation, no token, unreachable server).
Timing differences alone never produce exit 1.

On timing
---------
A timing comparison between two machines is a measurement, not a verdict.
Page cache state, concurrent load, JIT warmth and host hardware all move these
numbers around by more than the thing you are usually looking for.  Treat a
flagged ratio as "go look at this", never as a regression on its own.

Note also that /api/cached/* sits behind a ~24h in-memory cache inside
neuPrintHTTP, so timing those endpoints measures the cache, not the database,
unless the process was recently restarted.  They are marked `cached` in the
report for that reason.

On index usage
--------------
Deliberately NOT asserted.  Whether a query uses an index seek or a label scan
is a planner cost decision, not a correctness property -- an earlier checker in
this project asserted a ROI query produces a NodeIndexSeek and was simply
wrong.  What IS compared is the index *inventory* (`SHOW INDEXES`): names,
properties, state and population.  A difference there is real; a different plan
for the same data is not necessarily anything.

Auth
----
NEUPRINT_JWT or NEUPRINT_TOKEN, sent in a request header only -- never printed,
never written to the report, never placed on a command line.  If a server needs
its own token, set NEUPRINT_JWT_<SERVER> with the hostname uppercased and
non-alphanumerics turned into underscores, e.g. NEUPRINT_JWT_NEUPRINT_PRE.

Everything here is read-only.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.style import (Reporter, Style, add_baseline_argument,  # noqa: E402
                          add_color_argument, load_baseline, want_color)

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / 'results'        # one results/ dir shared by every tool
TIMEOUT = 300

SEARCH_PROPS = [
    'type', 'instance', 'hemibrainType', 'flywireType', 'systematicType',
    'itoleeHl', 'trumanHl', 'synonyms', 'class', 'entryNerve', 'exitNerve',
]

# Rows whose order is not meaningful still have to compare equal, but an
# ordering change is worth saying out loud rather than hiding, so both the
# ordered and the unordered digest are kept.
FLOAT_PLACES = 6


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

# Terminal styling, the findings roll-up and plain-text report writing all
# live in common/style.py so that every tool in this repo reports the same
# way -- see that module for what each color means and why color codes
# must go at the end of a formatted line.

def normalize_server(arg):
    if arg.startswith(('http://', 'https://')):
        return arg.rstrip('/')
    if '.' in arg:
        return f'https://{arg}'
    return f'https://{arg}.janelia.org'


def server_label(server):
    host = urllib.parse.urlparse(server).hostname or server
    return host.split('.')[0]


def token_for(server):
    """Per-server token if set, else the shared one."""
    host = urllib.parse.urlparse(server).hostname or server
    key = 'NEUPRINT_JWT_' + re.sub(r'[^A-Za-z0-9]+', '_', host.split('.')[0]).upper()
    return (os.environ.get(key)
            or os.environ.get('NEUPRINT_JWT')
            or os.environ.get('NEUPRINT_TOKEN'))


class Instance:
    """One (server, dataset) pair, with its own credentials."""

    def __init__(self, server, dataset):
        self.server = normalize_server(server)
        self.dataset = dataset
        self.token = token_for(self.server)
        self.label = f'{server_label(self.server)}:{dataset}'

    def request(self, method, path, body=None, params=None):
        """Return (payload, seconds, error).  payload is parsed JSON when the
        response is JSON, raw bytes otherwise."""
        url = self.server + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method)
        if self.token:
            req.add_header('Authorization', f'Bearer {self.token}')
        if body is not None:
            req.add_header('Content-Type', 'application/json')
            req.data = json.dumps(body).encode()
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(
                    req, timeout=TIMEOUT,
                    context=ssl.create_default_context()) as r:
                raw = r.read()
                elapsed = time.perf_counter() - t0
                ctype = r.headers.get('Content-Type', '')
                cenc = r.headers.get('Content-Encoding', '')
            # /api/cached/dailytype sets Content-Encoding: gzip and serves a
            # gzipped body. urllib does not decompress, so without this the
            # payload arrives as bytes starting 0x1f 0x8b and json.loads --
            # or any attempt to read it -- fails with a decode error that
            # looks like a server fault rather than our own.
            if 'gzip' in cenc.lower():
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass            # not actually gzipped; use it as-is
            if 'json' in ctype:
                return json.loads(raw), elapsed, None
            return raw, elapsed, None
        except urllib.error.HTTPError as e:
            # neuPrintHTTP answers 400 for every Cypher error, including a lost
            # Bolt connection, so the body says more than the status does.
            detail = e.read().decode('utf-8', 'replace')[:160].replace('\n', ' ')
            return None, time.perf_counter() - t0, f'HTTP {e.code}: {detail}'
        except Exception as e:                      # noqa: BLE001
            return None, time.perf_counter() - t0, str(e)

    def cypher(self, q):
        payload, _, err = self.request(
            'POST', '/api/custom/custom',
            body={'dataset': self.dataset, 'cypher': q})
        if err:
            return None, err
        return (payload or {}).get('data') or [], None

    def scalar(self, q, default=None):
        rows, err = self.cypher(q)
        if err or not rows:
            return default
        return rows[0][0]


# --------------------------------------------------------------------------
# normalizing and comparing payloads
# --------------------------------------------------------------------------

def canon(obj):
    """Recursively round floats so that two servers' float formatting cannot
    masquerade as a data difference."""
    if isinstance(obj, float):
        return round(obj, FLOAT_PLACES)
    if isinstance(obj, dict):
        return {k: canon(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [canon(v) for v in obj]
    return obj


def digest(obj):
    if isinstance(obj, (bytes, bytearray)):
        return hashlib.sha256(obj).hexdigest()[:16]
    blob = json.dumps(canon(obj), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def rows_of(payload):
    """The row list, for payloads shaped like neuPrintHTTP's {columns, data}."""
    if isinstance(payload, dict) and isinstance(payload.get('data'), list):
        return payload['data']
    return None


def unordered_digest(payload):
    rows = rows_of(payload)
    if rows is None:
        return None
    keyed = sorted(json.dumps(canon(r), sort_keys=True, default=str) for r in rows)
    return hashlib.sha256('\n'.join(keyed).encode()).hexdigest()[:16]


def sorted_deep(obj):
    """Every list sorted by its canonical form, recursively.

    Lets "same content, different order" be told apart from a real difference
    even when the list is buried inside a dict.  Needed because several
    neuPrintHTTP fields are built by ranging over a Go map -- see the ROIs
    list in storage/neuprintbolt -- and Go randomizes map iteration order, so
    those arrive in a different order on every call, including two calls to
    the same server.
    """
    if isinstance(obj, dict):
        return {k: sorted_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [sorted_deep(v) for v in obj]
        return sorted(items, key=lambda v: json.dumps(canon(v), sort_keys=True,
                                                      default=str))
    return obj


def shape(obj, depth=0):
    """Structure without values -- for responses that are legitimately
    non-deterministic, where only the shape can be compared."""
    if depth > 6:
        return '...'
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1), f'xN={len(obj)}'] if obj else []
    return type(obj).__name__


def size_of(payload):
    if payload is None:
        return 0
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)
    return len(json.dumps(payload, default=str))


# --------------------------------------------------------------------------
# endpoint catalog
# --------------------------------------------------------------------------
# Built from the handlers in neuPrintHTTP api/: the request shapes come from
# the *Params structs in api/npexplorer/storageapi.go, the cached and
# path-parameter endpoints from their own handlers.  `volatile` marks a
# response that is non-deterministic by design, so only its shape is compared.

FILTER = {'statuses': [], 'pre_threshold': 0, 'post_threshold': 0}


def endpoint_catalog(inputs):
    """inputs: the deterministic values chosen from both instances."""
    ds = '{dataset}'
    cat = [
        # --- dbmeta -----------------------------------------------------
        # ROIs and SuperLevelROIs are built by ranging over a Go map, so their
        # order is randomized per call and means nothing.
        dict(name='dbmeta/datasets', client='both', method='GET',
             path='/api/dbmeta/datasets', pick_dataset=True,
             unordered_lists=True),

        # --- cached (behind a ~24h in-process cache) --------------------
        dict(name='cached/roiconnectivity', client='explorer', method='GET',
             path='/api/cached/roiconnectivity', query={'dataset': ds},
             cached=True),
        dict(name='cached/roicompleteness', client='explorer', method='GET',
             path='/api/cached/roicompleteness', query={'dataset': ds},
             cached=True),
        dict(name='cached/dailytype', client='explorer', method='GET',
             path='/api/cached/dailytype', query={'dataset': ds},
             cached=True, volatile=True),

        # --- npexplorer, dataset-only bodies ----------------------------
        dict(name='npexplorer/roiconnectivity', client='explorer', method='POST',
             path='/api/npexplorer/roiconnectivity', body={'dataset': ds}),
        dict(name='npexplorer/neuronmeta', client='explorer', method='POST',
             path='/api/npexplorer/neuronmeta', body={'dataset': ds}),
        dict(name='npexplorer/completeness', client='explorer', method='POST',
             path='/api/npexplorer/completeness',
             body={'dataset': ds, **FILTER}),
        dict(name='npexplorer/autapses', client='explorer', method='POST',
             path='/api/npexplorer/autapses', body={'dataset': ds, **FILTER}),
        dict(name='npexplorer/neuronmetavals', client='explorer', method='POST',
             path='/api/npexplorer/neuronmetavals',
             body={'dataset': ds, 'key_name': 'type'}),

        # Registered in main.go rather than the api package, so it is easy to
        # miss when enumerating routes.  Drives the Neuroglancer layer setup.
        dict(name='npexplorer/nglayers', client='explorer', method='GET',
             path=f'/api/npexplorer/nglayers/{ds}', path_param=True),
    ]

    if inputs.get('roi'):
        cat.append(dict(
            name='npexplorer/distribution', client='explorer', method='POST',
            path='/api/npexplorer/distribution',
            body={'dataset': ds, 'ROI': inputs['roi'], 'is_pre': True}))
        cat.append(dict(
            name='roimeshes/mesh', client='both', method='GET',
            path=f'/api/roimeshes/mesh/{ds}/{urllib.parse.quote(inputs["roi"], safe="")}',
            path_param=True))

    if inputs.get('body_id') is not None:
        bid = inputs['body_id']
        cat.append(dict(
            name='npexplorer/simpleconnections', client='explorer', method='POST',
            path='/api/npexplorer/simpleconnections',
            body={'dataset': ds, 'neuron_id': str(bid), 'find_inputs': False,
                  **FILTER}))
        cat.append(dict(
            name='npexplorer/rankedtable', client='explorer', method='POST',
            path='/api/npexplorer/rankedtable',
            body={'dataset': ds, 'neuron_id': str(bid), 'find_inputs': False,
                  **FILTER}))
        cat.append(dict(
            name='npexplorer/roisinneuron', client='explorer', method='POST',
            path='/api/npexplorer/roisinneuron',
            body={'dataset': ds, 'neuron_id': str(bid)}))
        cat.append(dict(
            name='npexplorer/commonconnectivity', client='explorer', method='POST',
            path='/api/npexplorer/commonconnectivity',
            body={'dataset': ds, 'neuron_ids': [bid], 'find_inputs': False,
                  **FILTER}))
        cat.append(dict(
            name='skeletons/skeleton', client='both', method='GET',
            path=f'/api/skeletons/skeleton/{ds}/{bid}', path_param=True))

    if inputs.get('cell_type'):
        t = urllib.parse.quote(inputs['cell_type'], safe='')
        cat.append(dict(name='npexplorer/celltype', client='explorer', method='GET',
                        path=f'/api/npexplorer/celltype/{ds}/{t}', path_param=True))
        cat.append(dict(
            name='npexplorer/findneurons', client='explorer', method='POST',
            path='/api/npexplorer/findneurons',
            body={'dataset': ds, 'neuron_name': inputs['cell_type'],
                  'input_ROIs': [], 'output_ROIs': [], **FILTER}))

    return cat


def materialize(spec, inst, encode_dataset=False):
    """Substitute this instance's dataset into a spec.

    encode_dataset percent-encodes the name as a browser would when building
    a URL path -- `yakuba-vnc:v0.0` becomes `yakuba-vnc%3Av0.0`. See
    layer_path_encoding() for why that is worth testing separately.
    """
    name = (urllib.parse.quote(inst.dataset, safe='') if encode_dataset
            else inst.dataset)

    def sub(v):
        if isinstance(v, str):
            return v.replace('{dataset}', name)
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v
    return sub(spec.get('path')), sub(spec.get('body')), sub(spec.get('query'))


def call(spec, inst, encode_dataset=False):
    path, body, query = materialize(spec, inst, encode_dataset)
    return inst.request(spec['method'], path, body=body, params=query)


def needs_encoding(name):
    """True when percent-encoding the name as a URL path segment changes it.

    Only tagged dataset names do, since the tag separator is a colon:
    'yakuba-vnc' is unchanged, 'yakuba-vnc:v0.0' becomes 'yakuba-vnc%3Av0.0'.
    """
    return urllib.parse.quote(name, safe='') != name


# --------------------------------------------------------------------------
# choosing deterministic inputs present on both sides
# --------------------------------------------------------------------------

def choose_inputs(a, b, rep):
    """Pick a cell type, bodyId and ROI that exist on BOTH instances, so a
    difference in a response is never just a difference in the input."""
    inputs = {}

    types_a, _ = a.cypher(
        'MATCH (n:Neuron) WHERE n.type IS NOT NULL '
        'RETURN DISTINCT n.type ORDER BY n.type LIMIT 2000')
    types_b, _ = b.cypher(
        'MATCH (n:Neuron) WHERE n.type IS NOT NULL '
        'RETURN DISTINCT n.type ORDER BY n.type LIMIT 2000')
    common_types = sorted({r[0] for r in (types_a or [])}
                          & {r[0] for r in (types_b or [])})
    if common_types:
        inputs['cell_type'] = common_types[0]

    q_body = ('MATCH (n:Neuron) WHERE n.type IS NOT NULL '
              'RETURN n.bodyId ORDER BY n.bodyId LIMIT 1')
    ba, bb = a.scalar(q_body), b.scalar(q_body)
    if ba is not None and ba == bb:
        inputs['body_id'] = ba
    elif ba is not None and bb is not None:
        # Fall back to the lowest bodyId the two have in common.
        rows_a, _ = a.cypher('MATCH (n:Neuron) RETURN n.bodyId '
                             'ORDER BY n.bodyId LIMIT 5000')
        rows_b, _ = b.cypher('MATCH (n:Neuron) RETURN n.bodyId '
                             'ORDER BY n.bodyId LIMIT 5000')
        common = sorted({r[0] for r in (rows_a or [])}
                        & {r[0] for r in (rows_b or [])})
        if common:
            inputs['body_id'] = common[0]

    rois_a = a.scalar("MATCH (m:Meta) RETURN m.primaryRois") or []
    rois_b = b.scalar("MATCH (m:Meta) RETURN m.primaryRois") or []
    common_rois = sorted(set(rois_a) & set(rois_b))
    if common_rois:
        inputs['roi'] = common_rois[0]

    rep('  inputs chosen (present on both):')
    for k in ('cell_type', 'body_id', 'roi'):
        rep(f'    {k:<10} {inputs.get(k, "-- none in common --")}')
    return inputs


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

def row(rep, label, va, vb, note='', cat='data'):
    """One side-by-side line.  Color codes go only at the end of the line, so
    they never disturb the column alignment above."""
    same = str(va) == str(vb)
    mark = '' if same else rep.s.red('   <-- differs')
    if not same:
        rep.diff(f'{cat} / {label}: {va} vs {vb}')
    rep(f'  {label:<24} {str(va):>20} {str(vb):>20}{mark}{note}')
    return same


def layer_identity(a, b, rep):
    """Server and Neo4j versions.  These are EXPECTED to differ during a
    parallel release, so they are reported and never counted as failures."""
    rep('\n--- instance identity (differences here are expected) ---')
    for inst, side in ((a, 'A'), (b, 'B')):
        ver, _, _ = inst.request('GET', '/api/version')
        neo = inst.scalar('CALL dbms.components() YIELD versions '
                          'RETURN versions[0]')
        v = (ver or {}).get('Version', '?') if isinstance(ver, dict) else '?'
        rep(f'  {side}  {inst.label:<28} neuPrintHTTP {v:<10} neo4j {neo}')


def layer_data(a, b, rep):
    """The original comparison: provenance, counts, coverage, types."""
    same = True
    rep('\n--- data ---')
    rep(f'  {"":<24} {a.label:>20} {b.label:>20}')

    rep('\n  :Meta')
    meta_q = ('MATCH (m:Meta) RETURN m.uuid, m.lastDatabaseEdit, '
              'm.totalPreCount, m.totalPostCount')
    ma, ea = a.cypher(meta_q)
    mb, eb = b.cypher(meta_q)
    if ea or eb:
        rep(rep.s.red(f'  ERROR reading :Meta -- {ea or eb}'))
        rep.fatal(f'cannot read :Meta from '
                  f'{a.label if ea else b.label} -- {ea or eb}')
        return False
    ma = (ma or [[None] * 4])[0]
    mb = (mb or [[None] * 4])[0]
    for i, name in enumerate(['uuid', 'lastDatabaseEdit',
                              'totalPreCount', 'totalPostCount']):
        va, vb = ma[i], mb[i]
        if name == 'uuid':
            va, vb = str(va)[:12] + '...', str(vb)[:12] + '...'
        if name == 'lastDatabaseEdit':
            va, vb = str(va)[:19], str(vb)[:19]
        same &= row(rep, name, va, vb, cat='meta')

    rep('\n  node counts')
    for label in ['Segment', 'Neuron', 'Synapse', 'SynapseSet']:
        q = f'MATCH (n:{label}) RETURN count(n)'
        ca, cb = a.scalar(q, 0), b.scalar(q, 0)
        delta = f'  ({cb - ca:+d})' if ca != cb else ''
        same &= row(rep, label, ca, f'{cb}{delta}', cat='nodes')

    rep('\n  Neurons carrying each searched property')
    counts = ', '.join(f'count(n.`{p}`)' for p in SEARCH_PROPS)
    pa, _ = a.cypher(f'MATCH (n:Neuron) RETURN {counts}')
    pb, _ = b.cypher(f'MATCH (n:Neuron) RETURN {counts}')
    if pa and pb:
        pa, pb = pa[0], pb[0]
        for i, p in enumerate(SEARCH_PROPS):
            if pa[i] or pb[i]:
                same &= row(rep, p, pa[i], pb[i], cat='property')
        empty = [p for i, p in enumerate(SEARCH_PROPS) if not pa[i] and not pb[i]]
        if empty:
            rep(rep.s.dim(f'  (null in both: {", ".join(empty)})'))

    rep('\n  types')
    q = ('MATCH (n:Neuron) WHERE n.type IS NOT NULL '
         'RETURN n.type, count(*) ORDER BY n.type')
    ta, _ = a.cypher(q)
    tb, _ = b.cypher(q)
    da = {r[0]: r[1] for r in (ta or [])}
    db = {r[0]: r[1] for r in (tb or [])}
    only_a = sorted(set(da) - set(db))
    only_b = sorted(set(db) - set(da))
    moved = sorted(t for t in set(da) & set(db) if da[t] != db[t])
    same &= row(rep, 'distinct types', len(da), len(db), cat='types')
    same &= row(rep, 'bodies with a type', sum(da.values()), sum(db.values()),
                cat='types')
    for title, items, src, side in ((f'only in {a.label}', only_a, da, a.label),
                                    (f'only in {b.label}', only_b, db, b.label)):
        count = rep.s.red(str(len(items))) if items else rep.s.green('0')
        rep(f'  {title}: {count}')
        if items:
            rep.diff(f'types / only in {side}: {len(items)}')
        for t in items[:12]:
            rep(f'      {t}  ({src[t]} bodies)')
        if len(items) > 12:
            rep(f'      ... and {len(items) - 12} more')
    moved_n = rep.s.red(str(len(moved))) if moved else rep.s.green('0')
    rep(f'  body count changed: {moved_n}')
    if moved:
        rep.diff(f'types / types changed body count: {len(moved)}')
    for t in moved[:12]:
        rep(f'      {t}  {da[t]} -> {db[t]}')
    if len(moved) > 12:
        rep(f'      ... and {len(moved) - 12} more')
    same &= not (only_a or only_b or moved)
    return same


def layer_indexes(a, b, rep):
    """Index INVENTORY, not index usage.  See the module docstring."""
    rep('\n--- index inventory ---')
    q = ('SHOW INDEXES YIELD name, labelsOrTypes, properties, state, type '
         'RETURN name, labelsOrTypes, properties, state, type ORDER BY name')
    ia, ea = a.cypher(q)
    ib, eb = b.cypher(q)
    if ea or eb:
        rep(rep.s.yellow(f'  skipped -- SHOW INDEXES unavailable ({ea or eb})'))
        rep.warn('indexes / SHOW INDEXES unavailable, inventory not compared')
        return True

    # Neo4j renamed the ordinary index type between generations: 4.4 reports
    # BTREE, 5+ and CalVer report RANGE. They are the same index. Comparing
    # the raw type name makes every index look unique to one side on any
    # 4.4-vs-CalVer comparison -- 128 phantom differences each way -- which is
    # precisely the comparison this tool exists to make.
    EQUIVALENT_TYPES = {'BTREE': 'RANGE'}

    def key(r):
        idx_type = str(r[4]).upper()
        return (str(r[1]), str(r[2]), EQUIVALENT_TYPES.get(idx_type, idx_type))

    ka = {key(r): r for r in (ia or [])}
    kb = {key(r): r for r in (ib or [])}
    row(rep, 'total indexes', len(ka), len(kb), cat='indexes')

    only_a = sorted(set(ka) - set(kb))
    only_b = sorted(set(kb) - set(ka))
    not_online_a = [r[0] for r in (ia or []) if r[3] != 'ONLINE']
    not_online_b = [r[0] for r in (ib or []) if r[3] != 'ONLINE']
    for title, items, side in ((f'only on {a.label}', only_a, a.label),
                               (f'only on {b.label}', only_b, b.label)):
        if items:
            rep(f'  {title}: {rep.s.red(str(len(items)))}')
            rep.diff(f'indexes / only on {side}: {len(items)}')
            for k in items[:10]:
                rep(f'      {k[0]} {k[1]} ({k[2]})')
            if len(items) > 10:
                rep(f'      ... and {len(items) - 10} more')
    for lbl, items in ((a.label, not_online_a), (b.label, not_online_b)):
        if items:
            rep(rep.s.red(f'  NOT ONLINE on {lbl}: {len(items)} -- {items[:5]}'))
            rep.diff(f'indexes / not ONLINE on {lbl}: {len(items)}')
    if not (only_a or only_b or not_online_a or not_online_b):
        rep(rep.s.green('  identical inventory, all ONLINE'))
    return not (only_a or only_b or not_online_a or not_online_b)


SETTINGS_OF_INTEREST = [
    # (label, candidate names newest-first -- 5.x renamed dbms.* to server.*)
    ('cypher language', ['db.query.default_language']),
    ('heap max', ['server.memory.heap.max_size', 'dbms.memory.heap.max_size']),
    ('page cache', ['server.memory.pagecache.size',
                    'dbms.memory.pagecache.size']),
    ('tx timeout', ['db.transaction.timeout', 'dbms.transaction.timeout']),
    ('read only', ['server.databases.default_to_read_only',
                   'dbms.databases.default_to_read_only']),
]


def read_settings(inst):
    """SHOW SETTINGS on 5.x and later, dbms.listConfig() before that."""
    rows, err = inst.cypher('SHOW SETTINGS YIELD name, value '
                            'RETURN name, value')
    if err:
        rows, err = inst.cypher('CALL dbms.listConfig() YIELD name, value '
                                'RETURN name, value')
    if err:
        return {}, err
    return {r[0]: r[1] for r in (rows or [])}, None


def layer_config(a, b, rep):
    """Server settings that can change answers without changing data.

    Reported, not asserted.  During a parallel release the two stacks are
    different Neo4j generations by design, so most of these differ for good
    reasons -- 4.4 does not even have the settings 5.x renamed.  The one that
    earns a warning is the Cypher language, because it changes query
    semantics rather than just resourcing.
    """
    rep('\n--- server configuration (reported, not asserted) ---')
    sa, ea = read_settings(a)
    sb, eb = read_settings(b)
    if ea and eb:
        rep(rep.s.yellow(f'  skipped -- settings unreadable ({ea})'))
        rep.warn('config / server settings could not be read on either side')
        return

    def lookup(settings, names):
        for n in names:
            if n in settings:
                return settings[n], n
        return None, None

    rep(f'  {"":<24} {a.label:>20} {b.label:>20}')
    for label, names in SETTINGS_OF_INTEREST:
        va, na = lookup(sa, names)
        vb, nb = lookup(sb, names)
        shown_a = '-- not set --' if va is None else str(va)
        shown_b = '-- not set --' if vb is None else str(vb)
        note = ''
        if na and nb and na != nb:
            # Same setting under its old and new name: not a difference.
            note = rep.s.dim(f'   ({na} vs {nb})')
        mark = '' if shown_a == shown_b else rep.s.yellow('   <-- differs')
        rep(f'  {label:<24} {shown_a:>20} {shown_b:>20}{mark}{note}')

    # APOC, which must match the server on its first two version numbers.
    apoc_a = a.scalar('RETURN apoc.version()') or '-- unavailable --'
    apoc_b = b.scalar('RETURN apoc.version()') or '-- unavailable --'
    mark = '' if apoc_a == apoc_b else rep.s.dim('   (expected to differ)')
    rep(f'  {"apoc":<24} {str(apoc_a):>20} {str(apoc_b):>20}{mark}')

    # The one that changes answers rather than resourcing.
    la, _ = lookup(sa, ['db.query.default_language'])
    lb, _ = lookup(sb, ['db.query.default_language'])
    for inst, lang in ((a, la), (b, lb)):
        if lang and 'CYPHER_5' not in str(lang):
            rep(rep.s.yellow(
                f'  NOTE {inst.label} serves {lang}, not CYPHER_5 -- the '
                f'pipeline pins CYPHER_5 and'))
            rep(rep.s.yellow(
                '       check-neuprint-snapshot asserts it, so the store was '
                'built and validated'))
            rep(rep.s.yellow('       under a different language than it is '
                             'being served under.'))
            rep.warn(f'config / {inst.label} serves {lang}, not CYPHER_5')
    if la and lb and la != lb:
        rep.warn(f'config / Cypher language differs: {la} vs {lb}')


def layer_connectivity(a, b, sample, rep):
    """Compare actual edges, not just how many there are.

    Node counts agreeing proves nothing about which bodies are joined to
    which, or at what weight: two databases can report the same relationship
    total while every edge in it differs.  This samples a deterministic set of
    bodies present on BOTH instances and compares their properties and their
    outgoing ConnectsTo edges, weights included.
    """
    rep(f'\n--- connectivity sample ({sample} bodies) ---')
    q = ('MATCH (n:Neuron) WHERE n.type IS NOT NULL '
         f'RETURN n.bodyId ORDER BY n.bodyId LIMIT {max(sample * 20, 500)}')
    ra, era = a.cypher(q)
    rb, erb = b.cypher(q)
    if era or erb:
        rep(rep.s.yellow(f'  skipped -- {era or erb}'))
        rep.warn('connectivity / body sample could not be drawn')
        return True

    common = sorted({r[0] for r in (ra or [])} & {r[0] for r in (rb or [])})
    bodies = common[:sample]
    if not bodies:
        rep(rep.s.yellow('  skipped -- no bodyIds in common'))
        rep.warn('connectivity / no bodyIds common to both instances')
        return True
    rep(rep.s.dim(f'  sampled bodyIds {bodies[0]} .. {bodies[-1]} '
                  f'({len(common)} candidates in common)'))

    idlist = '[' + ','.join(str(int(x)) for x in bodies) + ']'
    same = True

    # Per-body properties.
    pq = (f'MATCH (n:Neuron) WHERE n.bodyId IN {idlist} '
          'RETURN n.bodyId, n.type, n.status, n.pre, n.post ORDER BY n.bodyId')
    pa, _ = a.cypher(pq)
    pb, _ = b.cypher(pq)
    if digest(pa) == digest(pb):
        rep(f'  {"body properties":<24} {rep.s.green("identical")}  '
            f'({len(pa or [])} bodies)')
    else:
        same = False
        rep(f'  {"body properties":<24} {rep.s.red("DIFFERS")}')
        rep.diff('connectivity / sampled body properties differ')
        describe_difference({'data': pa}, {'data': pb}, rep)

    # Outgoing edges with weights.
    cq = (f'MATCH (n:Neuron)-[c:ConnectsTo]->(m) WHERE n.bodyId IN {idlist} '
          'RETURN n.bodyId, m.bodyId, c.weight '
          'ORDER BY n.bodyId, m.bodyId LIMIT 20000')
    ca, eca = a.cypher(cq)
    cb, ecb = b.cypher(cq)
    if eca or ecb:
        rep(rep.s.yellow(f'  edges skipped -- {eca or ecb}'))
        rep.warn('connectivity / ConnectsTo sample could not be read')
        return same

    wa = sum(r[2] or 0 for r in (ca or []))
    wb = sum(r[2] or 0 for r in (cb or []))
    row(rep, 'edges in sample', len(ca or []), len(cb or []),
        cat='connectivity')
    row(rep, 'summed weight', wa, wb, cat='connectivity')
    if digest(ca) == digest(cb):
        rep(f'  {"edge-by-edge":<24} {rep.s.green("identical")}')
    else:
        same = False
        rep(f'  {"edge-by-edge":<24} {rep.s.red("DIFFERS")}')
        rep.diff('connectivity / sampled ConnectsTo edges differ')
        describe_difference({'data': ca}, {'data': cb}, rep)
    return same


def layer_path_encoding(a, b, catalog, rep):
    """Do the path-parameter endpoints accept a correctly-encoded dataset name?

    Four endpoints put the dataset in the URL PATH rather than a query string.
    A browser building such a URL percent-encodes a colon, so a tagged dataset
    arrives as `yakuba-vnc%3Av0.0`. neuPrintHTTP reads the parameter with
    c.Param("dataset") and does not url.PathUnescape it, so the encoded form
    is treated as a literal dataset name that does not exist.

    Measured on neuprint-yakuba, 2026-10-21:
        /api/roimeshes/mesh/yakuba-vnc:v0.0/ANm     -> 200, 2,555,002 bytes
        /api/roimeshes/mesh/yakuba-vnc%3Av0.0/ANm   -> 400

    This is NOT a comparison between the two instances -- it is a property of
    each deployment -- but it belongs here because this is the tool that knows
    which endpoints take the dataset in the path. The rest of this tool sends
    the raw form, so without this check it reports those endpoints healthy
    while they are broken for any client that encodes correctly.

    Silent for untagged dataset names, where encoding changes nothing.
    """
    targets = [s for s in catalog if s.get('path_param')]
    instances = [i for i in (a, b) if needs_encoding(i.dataset)]
    if not targets or not instances:
        return
    rep.heading('--- dataset name in the URL path: raw vs encoded ---')
    rep(rep.s.dim('  A browser percent-encodes the colon in a tagged dataset '
                  'name. These endpoints'))
    rep(rep.s.dim('  take the dataset in the path, so both forms should '
                  'behave identically.'))
    for inst in instances:
        encoded = urllib.parse.quote(inst.dataset, safe='')
        rep(f'\n  {inst.label}   {inst.dataset} -> {encoded}')
        for spec in targets:
            _, _, raw_err = call(spec, inst)
            _, _, enc_err = call(spec, inst, encode_dataset=True)
            raw_s = raw_err.split(':')[0] if raw_err else 'ok'
            enc_s = enc_err.split(':')[0] if enc_err else 'ok'
            if raw_s == enc_s:
                rep(f'    {spec["name"]:<28} raw {raw_s:<10} '
                    f'encoded {enc_s:<10} {rep.s.green("same")}')
            else:
                rep(f'    {spec["name"]:<28} raw {raw_s:<10} '
                    f'encoded {enc_s:<10} {rep.s.yellow("DIFFERS")}')
                rep.warn(f'encoding / {spec["name"]} on {inst.label}: raw '
                         f'{raw_s}, encoded {enc_s} -- breaks clients that '
                         f'encode the path correctly')


def layer_endpoints(a, b, catalog, rep):
    """Call every endpoint on both and compare the responses."""
    rep('\n--- function: endpoint responses ---')
    rep(rep.s.dim('  client: E = neuPrintExplorer only, P = neuprint-python '
                  'only, EP = both'))
    rep(f'  {"endpoint":<30} {"cl":<3} {"A":>10} {"B":>10}  verdict')
    all_same = True
    results = {}
    for spec in catalog:
        name = spec['name']
        pa, _, ea = call(spec, a)
        pb, _, eb = call(spec, b)
        results[name] = (pa, pb, ea, eb)

        sa = f'{size_of(pa)}B' if ea is None else 'ERR'
        sb = f'{size_of(pb)}B' if eb is None else 'ERR'
        tag = ' [cached]' if spec.get('cached') else ''
        # Which client drives this endpoint, so a difference can be
        # traced to the thing that will actually break.
        cl = {'explorer': 'E', 'python': 'P',
              'both': 'EP'}.get(spec.get('client', ''), '?')

        if ea or eb:
            # Both sides failing the same way is not a difference between
            # them -- it usually means the endpoint is unconfigured here, or
            # broken for both.  Worth saying, not worth calling a divergence.
            if ea and eb and error_class(ea) == error_class(eb):
                rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  '
                    f'{rep.s.yellow("both failed identically")}{tag}')
                rep.warn(f'endpoint / {name}: fails on BOTH -- {ea[:70]}')
                rep(rep.s.dim(f'      {ea[:150]}'))
                continue
            all_same = False
            rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  {rep.s.red("ERROR")}{tag}')
            rep.diff(f'endpoint / {name}: '
                     + ('failed on BOTH sides differently' if ea and eb
                        else f'failed on {a.label if ea else b.label} only'))
            if ea:
                rep(rep.s.red(f'      A: {ea}'))
            if eb:
                rep(rep.s.red(f'      B: {eb}'))
            continue

        if spec.get('pick_dataset'):
            pa = pick_dataset_entry(pa, a.dataset)
            pb = pick_dataset_entry(pb, b.dataset)

        pa, dropped = strip_volatile_columns(pa)
        pb, _ = strip_volatile_columns(pb)
        if dropped:
            tag += rep.s.dim(f' [ignoring {", ".join(dropped)}]')

        if spec.get('volatile'):
            ok = digest(shape(pa)) == digest(shape(pb))
            verdict = (rep.s.green('same shape') if ok
                       else rep.s.red('SHAPE DIFFERS'))
            all_same &= ok
            if not ok:
                rep.diff(f'endpoint / {name}: response shape differs')
            rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  {verdict} '
                f'{rep.s.dim("(values vary by design)")}{tag}')
            continue

        if digest(pa) == digest(pb):
            if looks_empty(pa):
                rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  '
                    f'{rep.s.yellow("identical, but EMPTY on both")}{tag}')
                rep.warn(f'endpoint / {name}: both sides returned an empty '
                         f'response -- agreement here is not reassurance')
                continue
            rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  '
                f'{rep.s.green("identical")}{tag}')
            continue

        ua, ub = unordered_digest(pa), unordered_digest(pb)
        if ua is not None and ua == ub:
            rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  '
                f'{rep.s.yellow("same rows, DIFFERENT ORDER")}{tag}')
            rep.warn(f'endpoint / {name}: same rows in a different order')
            all_same = False
            continue

        # Same content with some list reordered.  For an endpoint whose order
        # is known to be arbitrary this is not a finding at all; elsewhere it
        # is worth a warning but not a failure.
        if digest(sorted_deep(pa)) == digest(sorted_deep(pb)):
            if spec.get('unordered_lists'):
                rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  '
                    f'{rep.s.green("identical")} '
                    f'{rep.s.dim("(list order is arbitrary here)")}{tag}')
                continue
            rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  '
                f'{rep.s.yellow("same content, DIFFERENT LIST ORDER")}{tag}')
            rep.warn(f'endpoint / {name}: same content, a list reordered')
            all_same = False
            continue

        all_same = False
        rep(f'  {name:<30} {cl:<3} {sa:>10} {sb:>10}  {rep.s.red("DIFFERS")}{tag}')
        rep.diff(f'endpoint / {name}: response content differs')
        describe_difference(pa, pb, rep)
    return all_same, results


# Columns carrying Neo4j internal node ids.  neuPrintHTTP's RankedTableQuery
# returns `id(m) AS m_id, id(n) AS n_id, id(startNode(e)) AS pre_id`, and an
# internal id is assigned at import time from insertion order -- so two
# separately built databases disagree on them however identical their data.
# Comparing them would report a difference on every parallel release.
VOLATILE_COLUMNS = {'m_id', 'n_id', 'pre_id'}


def strip_volatile_columns(payload):
    """Drop internal-id columns from a {columns, data} payload."""
    if not (isinstance(payload, dict) and isinstance(payload.get('data'), list)
            and isinstance(payload.get('columns'), list)):
        return payload, []
    cols = payload['columns']
    drop = [i for i, c in enumerate(cols) if c in VOLATILE_COLUMNS]
    if not drop:
        return payload, []
    keep = [i for i in range(len(cols)) if i not in drop]
    out = dict(payload)
    out['columns'] = [cols[i] for i in keep]
    out['data'] = [[r[i] for i in keep if i < len(r)]
                   if isinstance(r, list) else r
                   for r in payload['data']]
    return out, [cols[i] for i in drop]


def error_class(err):
    """Two errors are 'the same failure' if they share a status code and the
    same leading message.  Bodies can carry a dataset name or a node id that
    legitimately differs between instances, so compare the kind, not the text.
    """
    status = re.match(r'HTTP (\d+)', err or '')
    head = re.sub(r'[0-9]+', 'N', (err or ''))[:90]
    return (status.group(1) if status else '?', head)


def looks_empty(payload):
    """True when a response carries no actual content.

    Two instances returning the same empty answer compare as identical, which
    is correct but can read as reassurance when it is the opposite -- the
    overviewRois defect made /api/cached/roiconnectivity return an empty ROI
    list on every CalVer server, so two of them agreed perfectly while both
    were broken.
    """
    if payload is None:
        return True
    if isinstance(payload, (bytes, bytearray)):
        return len(payload) == 0
    if isinstance(payload, dict):
        inner = [v for k, v in payload.items() if k != 'columns']
        return bool(inner) and all(
            v in (None, [], {}, '') for v in inner)
    if isinstance(payload, list):
        return not payload
    return False


def pick_dataset_entry(payload, dataset):
    """/api/dbmeta/datasets returns every dataset; compare only ours.

    Return the entry's *contents*, not `{name: contents}`.  Keeping the name
    as a key makes the comparison fail whenever the two sides are different
    datasets -- which is the whole point of the legacy same-server mode -- so
    it would report a difference that is only ever the key itself.
    """
    if isinstance(payload, dict) and dataset in payload:
        return payload[dataset]
    return payload


def describe_difference(pa, pb, rep, limit=4):
    ra, rb = rows_of(pa), rows_of(pb)
    if ra is not None and rb is not None:
        rep(f'      rows: {len(ra)} vs {len(rb)}')
        # Name the columns that moved.  A bare list of eleven values tells you
        # something differs but not what, which is the part you need.
        cols = pa.get('columns') if isinstance(pa, dict) else None
        shown = 0
        for i, (x, y) in enumerate(zip(ra, rb)):
            if canon(x) == canon(y):
                continue
            if cols and isinstance(x, list) and isinstance(y, list):
                moved = [(cols[j] if j < len(cols) else f'col{j}', u, v)
                         for j, (u, v) in enumerate(zip(x, y))
                         if canon(u) != canon(v)]
                rep(f'      row {i}: ' + ', '.join(
                    f'{c}: {u} -> {v}' for c, u, v in moved[:6]))
            else:
                rep(f'      row {i} A: {str(x)[:90]}')
                rep(f'      row {i} B: {str(y)[:90]}')
            shown += 1
            if shown >= limit:
                rep('      ...')
                break
        return
    if isinstance(pa, dict) and isinstance(pb, dict):
        keys = sorted(set(pa) | set(pb))
        shown = 0
        for k in keys:
            if canon(pa.get(k)) != canon(pb.get(k)):
                rep(f'      key {k!r} A: {str(pa.get(k))[:80]}')
                rep(f'      key {k!r} B: {str(pb.get(k))[:80]}')
                shown += 1
                if shown >= limit:
                    rep('      ...')
                    break
        return
    rep(f'      A digest {digest(pa)}  B digest {digest(pb)}  '
        f'({size_of(pa)}B vs {size_of(pb)}B)')


def layer_timing(a, b, catalog, reps, tolerance, rep):
    """Interleaved A,B,A,B with the first repetition discarded."""
    rep(f'\n--- timing ({reps} reps, first discarded, median reported) ---')
    rep(f'  {"endpoint":<30} {"A med":>9} {"B med":>9} {"B/A":>7}')
    flagged = []
    for spec in catalog:
        ta, tb = [], []
        failed = False
        for _ in range(reps):
            _, sa, ea = call(spec, a)
            _, sb, eb = call(spec, b)
            if ea or eb:
                failed = True
                break
            ta.append(sa)
            tb.append(sb)
        if failed or len(ta) < 2:
            why = 'error' if failed else 'too few samples'
            rep(f'  {spec["name"]:<30} {"--":>9} {"--":>9} {"--":>7}  '
                f'{rep.s.yellow(why)}')
            continue
        ma, mb = statistics.median(ta[1:]), statistics.median(tb[1:])
        ratio = (mb / ma) if ma > 0 else float('inf')
        mark = ''
        if ratio > tolerance or ratio < 1 / tolerance:
            mark = rep.s.yellow('   <-- outside tolerance')
            flagged.append((spec['name'], ratio))
            rep.warn(f'timing / {spec["name"]}: B/A {ratio:.2f}x'
                     + (' [cached]' if spec.get('cached') else ''))
        tag = rep.s.dim(' [cached]') if spec.get('cached') else ''
        rep(f'  {spec["name"]:<30} {ma * 1000:>8.0f}m {mb * 1000:>8.0f}m '
            f'{ratio:>7.2f}{mark}{tag}')
    if flagged:
        rep(rep.s.yellow(
            f'\n  {len(flagged)} endpoint(s) outside the {tolerance}x band.') +
            ' This is a measurement, not a verdict --')
        rep('  page cache, host load and hardware move these more than most '
            'regressions do.')
    return flagged


# --------------------------------------------------------------------------

def parse_endpoints(argv_rest):
    """Accept either `A B` with server[:dataset], or the legacy
    `server ds-a ds-b` three-argument form."""
    if len(argv_rest) == 3 and ':' not in argv_rest[0]:
        server, ds_a, ds_b = argv_rest
        return Instance(server, ds_a), Instance(server, ds_b)
    if len(argv_rest) == 2:
        first, second = argv_rest
        if ':' not in first:
            raise SystemExit('ERROR: A must be server:dataset (or use the '
                             'three-argument form: server ds-a ds-b)')
        sa, ds_a = first.split(':', 1)
        if ':' in second:
            sb, ds_b = second.split(':', 1)
        else:
            sb, ds_b = second, ds_a
        return Instance(sa, ds_a), Instance(sb, ds_b)
    raise SystemExit('ERROR: expected "A B" or "server ds-a ds-b". '
                     'Run with --help.')


def main(argv):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('targets', nargs='*', metavar='TARGET')
    p.add_argument('--reps', type=int, default=4,
                   help='timing repetitions per endpoint (default 4; the '
                        'first is discarded as warm-up)')
    p.add_argument('--tolerance', type=float, default=2.0,
                   help='flag timing ratios outside this band (default 2.0)')
    p.add_argument('--no-timing', action='store_true')
    p.add_argument('--no-endpoints', action='store_true')
    p.add_argument('--no-indexes', action='store_true')
    p.add_argument('--no-config', action='store_true')
    p.add_argument('--no-connectivity', action='store_true')
    p.add_argument('--sample', type=int, default=25, metavar='N',
                   help='bodies to sample for the connectivity comparison '
                        '(default 25; chosen from bodyIds present on both)')
    p.add_argument('--no-report', action='store_true',
                   help='do not write results/<a>-vs-<b>-<stamp>.txt')
    add_color_argument(p)
    add_baseline_argument(p)
    args = p.parse_args(argv[1:])

    if not args.targets:
        print(__doc__)
        return 2

    a, b = parse_endpoints(args.targets)
    if not a.token or not b.token:
        print('ERROR: set NEUPRINT_JWT (or NEUPRINT_TOKEN). An anonymous '
              'client is shown fewer datasets and may not see these at all.',
              file=sys.stderr)
        return 2

    rep = Reporter(Style(want_color(args.color)),
                   baseline=load_baseline(args.baseline))
    started = datetime.now(timezone.utc)
    rep(f'compare-datasets  {started.strftime("%Y-%m-%d %H:%M:%SZ")}')
    rep(f'  A  {a.server}  dataset {a.dataset}')
    rep(f'  B  {b.server}  dataset {b.dataset}')

    layer_identity(a, b, rep)

    data_same = layer_data(a, b, rep)

    # If the data layer could not even read :Meta, one side is unreachable or
    # the dataset is not served there.  Calling eighteen endpoints against it
    # would bury the real reason under a wall of identical failures.
    if rep.fatals:
        rep(rep.s.yellow('\n  remaining layers skipped -- see "could not '
                         'complete" below'))
        args.no_indexes = args.no_endpoints = args.no_timing = True
        args.no_config = args.no_connectivity = True

    conn_same = True if args.no_connectivity else layer_connectivity(
        a, b, args.sample, rep)
    index_same = True if args.no_indexes else layer_indexes(a, b, rep)
    if not args.no_config:
        layer_config(a, b, rep)

    endpoints_same = True
    if not args.no_endpoints or not args.no_timing:
        rep('\n--- choosing comparison inputs ---')
        inputs = choose_inputs(a, b, rep)
        catalog = endpoint_catalog(inputs)
    if not args.no_endpoints:
        endpoints_same, _ = layer_endpoints(a, b, catalog, rep)
        # Only runs when a dataset name actually needs encoding.
        layer_path_encoding(a, b, catalog, rep)
    if not args.no_timing:
        layer_timing(a, b, catalog, args.reps, args.tolerance, rep)

    rep('\n=== summary ===')
    for name, enabled, good in (('data', True, data_same),
                                ('connectivity sample',
                                 not args.no_connectivity, conn_same),
                                ('index inventory', not args.no_indexes,
                                 index_same),
                                ('endpoint responses', not args.no_endpoints,
                                 endpoints_same)):
        if rep.fatals:
            rep(f'  {name:<22} {rep.s.yellow("not compared")}')
            continue
        if not enabled:
            rep(f'  {name:<22} {rep.s.dim("skipped")}')
            continue
        # Neutral wording on purpose: whether a flagged layer is a failure is
        # decided by the findings below -- it may be only warnings, or a
        # difference the baseline accepts -- and RESULT says which.
        rep(f'  {name:<22} '
            f'{rep.s.green("identical") if good else rep.s.yellow("see findings")}')

    # Shared, so the roll-up, the baseline and the "how to accept this"
    # hint behave identically in every tool that reports findings.
    n_diff, n_warn = rep.print_findings()

    if rep.fatals:
        rep(f'\n--- could not complete ({len(rep.fatals)}) ---')
        for text in rep.fatals:
            rep(f'  {rep.s.red("ABORT")}  {text}')
        rep(f'\nRESULT: {rep.s.red("INCOMPLETE -- nothing was compared")}')
        rep(rep.s.dim('This is not evidence that the instances differ.'))
        exit_code = 2
    else:
        # The verdict follows the findings, not the per-layer booleans.
        # Two consequences, both intended:
        #   * a difference accepted by the baseline does not fail the run,
        #     which is the whole point of having one;
        #   * a WARNING no longer fails it either, so the WARN/DIFF
        #     distinction finally means something. "Same rows, different
        #     order" is not a difference in the data, and failing on it
        #     trains you to ignore a red verdict.
        ok = n_diff == 0
        verdict = (rep.s.green('OK -- instances look equivalent') if ok
                   else rep.s.red('DIFFERENCES FOUND -- see findings above'))
        rep(f'\nRESULT: {verdict}')
        rep(rep.s.dim('(Timing differences are reported as warnings but never '
                      'change this verdict.)'))
        exit_code = 0 if ok else 1

    if not args.no_report:
        stamp = started.strftime('%Y%m%d-%H%M%S')
        path = RESULTS / f'{a.label}-vs-{b.label}-{stamp}.txt'.replace('/', '_')
        rep.write(path)
        print(f'\nreport: {path}')
    return exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv))

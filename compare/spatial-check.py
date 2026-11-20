#!/usr/bin/env python3
"""
spatial-check.py <server> <datasetA> <datasetB> [options]

Spatial queries on two datasets, side by side: do they return the same
synapses, how long do they take, and is the POINT index on `location`
actually used?

    ./spatial-check.py neuprint-fish2 fish2 fish2:v0.0
    ./spatial-check.py neuprint-fish2 fish2 fish2:v0.0 --point 56549 32907 10140 --radius 500
    ./spatial-check.py neuprint-pre wasp3:v0.8 wasp3:v0.0 --reps 5

Why: the ingest creates a POINT index `<ds>ElementLocation` on
`<ds>_Element(location)`, and the client's spatial plugin asks for elements
within a distance of a point. Neo4j 4.4 can create that index but cannot use
it in a query ("future index": created and dropped for migration only), so
on 4.4 every spatial query is a full scan of the Element label. From Neo4j 5
onward the index serves `point.distance()` predicates. This is the one place
where a CalVer store should be dramatically faster, and it is also where the
spelling changed: `distance()` on 3.5 and 4.4, `point.distance()` on 5+.

What it does, per dataset:

  1. reads the Neo4j version and picks the distance function for it;
  2. lists the indexes on `location`, with `readCount` where the server
     reports it (5+), before and after the queries, so index use is a
     measured fact rather than an inference. Neo4j flushes index usage
     statistics asynchronously, a few seconds behind the queries, so the
     "after" read is retried for up to --settle seconds until the counter
     moves; if it never does, the timing gap to the control query decides,
     with a warning;
  3. runs three queries `--reps` times and reports the median:
       count      elements within --radius of --point
       synapses   the synapses in that sphere, with type and confidence,
                  ordered by coordinate (--limit rows)
       control    the same count with an abs() bounding box, which no index
                  can serve, so the gap between it and `count` is what the
                  index buys;
  4. compares the synapse list between A and B: same set, or not, with the
     first few differences shown.

Radius and point are in the dataset's coordinate units (voxels: see
Meta.voxelSize, printed). With no --point, a synapse from dataset A is used,
so the sphere is somewhere with data in it.

A query that takes longer than --slow seconds on its first run is not
repeated: on a 4.4 store the scan can take minutes, and neuPrintHTTP's
gateway gives up at about 180 s, which is reported as such.

Auth: NEUPRINT_JWT or NEUPRINT_TOKEN, sent in a header only. Read-only.
Exit 0 if the synapse lists match and every 5+ store used its index, 1 if
they differ or an index that should serve the query did not, 2 if it could
not run.
"""
import argparse
import json
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.style import Style, add_color_argument, want_color  # noqa: E402

TIMEOUT = 300


def server_url(s):
    if s.startswith(('http://', 'https://')):
        return s
    return f'https://{s}.janelia.org' if '.' not in s else f'https://{s}'


class Store:
    def __init__(self, server, dataset, token):
        self.server, self.dataset, self.token = server, dataset, token
        self.prefix = dataset.split(':', 1)[0]
        self.version = None
        self.fn = None

    def cypher(self, q):
        """(rows, seconds, error). Rows are lists in column order."""
        req = urllib.request.Request(f'{self.server}/api/custom/custom', method='POST')
        req.add_header('Authorization', f'Bearer {self.token}')
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps({'dataset': self.dataset, 'cypher': q}).encode()
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
                return json.loads(r.read()).get('data') or [], time.time() - t0, None
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')
            if e.code == 504:
                return None, time.time() - t0, 'HTTP 504: gateway timeout (neuPrintHTTP gives up at ~180 s)'
            return None, time.time() - t0, f'HTTP {e.code}: {body[:160]}'
        except Exception as e:                                  # noqa: BLE001
            return None, time.time() - t0, f'{type(e).__name__}: {e}'

    def describe(self):
        rows, _, err = self.cypher('CALL dbms.components() YIELD versions RETURN versions[0]')
        if err:
            return err
        self.version = rows[0][0]
        major = self.version.split('.')[0]
        # 3.5 and 4.4 know distance(); 5.x and CalVer (2026.x) know point.distance().
        self.fn = 'distance' if major in ('3', '4') else 'point.distance'
        return None

    def location_indexes(self):
        """[(name, type, state, readCount or None)] for indexes on `location`."""
        rows, _, err = self.cypher(
            "SHOW INDEXES YIELD name, type, state, properties, readCount "
            "WHERE 'location' IN properties RETURN name, type, state, readCount")
        if err and 'readCount' in err:
            rows, _, err = self.cypher(
                "SHOW INDEXES YIELD name, type, state, properties "
                "WHERE 'location' IN properties RETURN name, type, state")
            rows = [r + [None] for r in rows] if rows else rows
        if err:
            return None, err
        return [tuple(r) for r in rows], None

    def voxel(self):
        rows, _, err = self.cypher('MATCH (m:Meta) RETURN m.voxelSize, m.voxelUnits')
        if err or not rows:
            return ''
        size, units = rows[0]
        return f'{"x".join(str(v) for v in size)} {units}' if size else ''

    def sample_point(self):
        rows, _, err = self.cypher(
            f'MATCH (s:{self.prefix}_Synapse) RETURN s.location.x, s.location.y, s.location.z SKIP 500000 LIMIT 1')
        if err or not rows:
            rows, _, err = self.cypher(
                f'MATCH (s:{self.prefix}_Synapse) RETURN s.location.x, s.location.y, s.location.z LIMIT 1')
        if err or not rows:
            return None
        return [int(v) for v in rows[0]]


def queries(st, p, radius, limit):
    x, y, z = p
    pt = f'point({{x:{x}, y:{y}, z:{z}}})'
    el, sy = f'{st.prefix}_Element', f'{st.prefix}_Synapse'
    return {
        'count': (f'MATCH (n:{el}) WHERE {st.fn}({pt}, n.location) < {radius} RETURN count(n)'),
        'synapses': (f'MATCH (n:{el}) WHERE n:{sy} AND {st.fn}({pt}, n.location) < {radius} '
                     f'RETURN n.location.x AS x, n.location.y AS y, n.location.z AS z, n.type AS type, '
                     f'n.confidence AS confidence ORDER BY x, y, z LIMIT {limit}'),
        'control': (f'MATCH (n:{el}) WHERE abs(n.location.x - {x}) < {radius} AND abs(n.location.y - {y}) < {radius} '
                    f'AND abs(n.location.z - {z}) < {radius} RETURN count(n)'),
    }


def timed(st, q, reps, slow):
    """Median seconds over reps (a slow first run is not repeated), rows of the last run, error."""
    times, rows = [], None
    for i in range(reps):
        r, secs, err = st.cypher(q)
        if err:
            return None, None, err, i + 1
        times.append(secs)
        rows = r
        if i == 0 and secs > slow:
            break
    return statistics.median(times), rows, None, len(times)


def measure(st, point, args, s):
    """Run the three queries and the index bookkeeping for one store; return a dict of facts."""
    f = dict(version=st.version, fn=st.fn, errors=[])
    before, err = st.location_indexes()
    f['indexes'] = before or []
    if err:
        f['errors'].append(f'could not list indexes: {err[:100]}')
    res = {}
    for name, q in queries(st, point, args.radius, args.limit).items():
        med, rows, qerr, n = timed(st, q, args.reps, args.slow)
        if qerr:
            f['errors'].append(f'{name}: {qerr}')
            res[name] = None
        else:
            res[name] = (med, rows, n)
    f['res'] = res

    # Index usage statistics are flushed a few seconds behind the queries
    # (measured: ~5 s on 2026.08.1), so poll until the counter moves.
    f['used'] = None            # True / False / None (not measurable)
    f['hits'] = 0
    if before and any(b[3] is not None for b in before):
        t0 = time.time()
        delta = {}
        while True:
            after, err = st.location_indexes()
            if after:
                delta = {a[0]: (a[3] or 0) - (b[3] or 0) for a, b in zip(after, before)
                         if a[3] is not None and b[3] is not None}
                if sum(delta.values()) > 0 or time.time() - t0 > args.settle:
                    break
            elif err:
                break
            time.sleep(2)
        f['hits'] = sum(delta.values())
    c, k = res.get('control'), res.get('count')
    f['gain'] = (c[0] / max(k[0], 1e-9)) if c and k else None
    major = (st.version or '0').split('.')[0]
    if major in ('3', '4'):
        f['used'] = None
        f['used_text'] = f'cannot be, on {st.version}'
    elif f['hits'] > 0:
        f['used'] = True
        f['used_text'] = f'yes, readCount rose by {f["hits"]}'
    elif f['gain'] is not None and f['gain'] >= 5:
        f['used'] = True
        f['used_text'] = f'yes, by timing ({f["gain"]:.0f}x faster than the control; readCount not flushed)'
    elif k:
        f['used'] = False
        f['used_text'] = f'NO: readCount unchanged after {args.settle:g} s'
    else:
        f['used'] = None
        f['used_text'] = 'not measured'
    return f


def main(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('server')
    p.add_argument('dataset_a')
    p.add_argument('dataset_b')
    p.add_argument('--point', nargs=3, type=int, metavar=('X', 'Y', 'Z'),
                   help='center, in the dataset coordinate units (default: a synapse from dataset A)')
    p.add_argument('--radius', type=int, default=200, help='in coordinate units (default 200)')
    p.add_argument('--reps', type=int, default=3, help='runs per query; the median is reported (default 3)')
    p.add_argument('--limit', type=int, default=5000, help='rows of the synapse list to compare (default 5000)')
    p.add_argument('--slow', type=float, default=30.0,
                   help='a first run slower than this many seconds is not repeated (default 30)')
    p.add_argument('--settle', type=float, default=30.0,
                   help='seconds to wait for readCount to catch up with the queries (default 30)')
    add_color_argument(p)
    if len(argv) == 1:
        print(__doc__)
        return 2
    args = p.parse_args(argv[1:])
    token = os.environ.get('NEUPRINT_JWT') or os.environ.get('NEUPRINT_TOKEN')
    if not token:
        print('ERROR: set NEUPRINT_JWT (or NEUPRINT_TOKEN).', file=sys.stderr)
        return 2
    s = Style(want_color(args.color))
    server = server_url(args.server)
    a, b = Store(server, args.dataset_a, token), Store(server, args.dataset_b, token)

    for st in (a, b):
        err = st.describe()
        if err:
            print(f'ERROR: {st.dataset} did not answer: {err}', file=sys.stderr)
            return 2
    point = args.point or a.sample_point()
    if not point:
        print('ERROR: no synapse found in dataset A to center on; pass --point.', file=sys.stderr)
        return 2
    voxel = a.voxel()
    print(f'server:  {server}')
    print(f'sphere:  center {tuple(point)}, radius {args.radius}'
          f'{" voxels of " + voxel if voxel else ""}'
          f'  ({"a synapse from " + a.dataset if not args.point else "given"})')
    print(f'reps:    {args.reps} per query, median reported; a first run over {args.slow:g} s is not repeated')
    print()

    fa, fb = measure(a, point, args, s), measure(b, point, args, s)

    # ---- the side-by-side table -------------------------------------------
    def secs(f, name):
        r = f['res'].get(name)
        if not r:
            return s.red('failed')
        med, _, n = r
        txt = f'{med:.2f} s' if med < 10 else f'{med:.1f} s'
        return txt if n == args.reps else txt + s.dim(' (1 run)')

    def count(f, name='count'):
        r = f['res'].get(name)
        return str(r[1][0][0]) if r else s.red('failed')

    # Same synapses?
    ra, rb = fa['res'].get('synapses'), fb['res'].get('synapses')
    key = lambda r: (r[0], r[1], r[2], r[3])                    # noqa: E731
    sa = {key(r) for r in ra[1]} if ra else None
    sb = {key(r) for r in rb[1]} if rb else None
    same = sa is not None and sb is not None and sa == sb
    if sa is None or sb is None:
        syn_a, syn_b = (s.red('failed') if sa is None else f'{len(sa)}'), (s.red('failed') if sb is None else f'{len(sb)}')
    elif same:
        syn_a = syn_b = s.green('identical set') + f' ({len(sa)})'
    else:
        syn_a = s.red(f'{len(sa)}, {len(sa - sb)} only here')
        syn_b = s.red(f'{len(sb)}, {len(sb - sa)} only here')

    def used(f):
        t = f['used_text']
        return s.green(t) if f['used'] else (s.red(t) if f['used'] is False else s.yellow(t))

    def index_row(f):
        if not f['indexes']:
            return s.yellow('none on location')
        return ', '.join(f'{n} {t} {st}' for n, t, st, _ in f['indexes'])

    ha, hb = f'{a.dataset} ({a.version})', f'{b.dataset} ({b.version})'
    rows = [
        ('distance function', f'{a.fn}()', f'{b.fn}()'),
        ('index on location', index_row(fa), index_row(fb)),
        ('elements in the sphere', count(fa), count(fb)),
        ('synapses, with type', syn_a, syn_b),
        ('indexed count', secs(fa, 'count'), secs(fb, 'count')),
        ('synapse list', secs(fa, 'synapses'), secs(fb, 'synapses')),
        ('unindexable control', secs(fa, 'control'), secs(fb, 'control')),
        ('POINT index used', used(fa), used(fb)),
    ]
    from common.style import strip_ansi
    w0 = max(len(r[0]) for r in rows)
    w1 = max([len(strip_ansi(ha))] + [len(strip_ansi(r[1])) for r in rows])
    pad = lambda t, w: t + ' ' * (w - len(strip_ansi(t)))      # noqa: E731
    print(f'  {"":<{w0}}  {s.cyan(pad(ha, w1))}  {s.cyan(hb)}')
    for label, va, vb in rows:
        print(f'  {label:<{w0}}  {pad(va, w1)}  {vb}')

    # ---- details only where something differs or failed --------------------
    print()
    exit_code = 0
    if sa is not None and sb is not None and not same:
        exit_code = 1
        for k in sorted(sa - sb)[:3]:
            print(f'  only in {a.dataset}: {k}')
        for k in sorted(sb - sa)[:3]:
            print(f'  only in {b.dataset}: {k}')
    elif same:
        conf_a = {key(r): r[4] for r in ra[1]}
        conf_b = {key(r): r[4] for r in rb[1]}
        diff_conf = [k for k in sa if conf_a[k] != conf_b[k]]
        if diff_conf:
            print(f'  {s.yellow("note")}: confidence differs on {len(diff_conf)} of the {len(sa)} synapses, '
                  f'e.g. {diff_conf[0]}: {conf_a[diff_conf[0]]} vs {conf_b[diff_conf[0]]}')
    for st, f in ((a, fa), (b, fb)):
        for e in f['errors']:
            print(f'  {s.red("error")} {st.dataset}: {e}')
        if f['used'] is False:
            exit_code = 1
    if any(f['res'].get(n) is None for f in (fa, fb) for n in ('count', 'synapses')):
        exit_code = max(exit_code, 1)

    if exit_code:
        print(f'RESULT: {s.red("DIFFERENCES")} -- see above.')
    else:
        print(f'RESULT: {s.green("OK")} -- same synapses on both, and every store that can use its POINT index did.')
    return exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv))

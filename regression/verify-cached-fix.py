#!/usr/bin/env python3
"""
verify-cached-fix.py <server> [dataset ...]

Verify the three Cypher fixes to neuPrintHTTP's api/cached/cached.go against
every Neo4j generation in the fleet, by running the old and new forms of each
query side by side on the live server.

    ./verify-cached-fix.py neuprint-pre hemibrain vnc wasp3:v0.8

Why: one neuPrintHTTP process serves Neo4j 3.5.3, 4.4.16 and 2026.08.1 at once,
so a fix has to be correct on the oldest and the newest simultaneously.  This
checks that directly rather than reasoning from a changelog.

What it expects to see:

  * On 3.5/4.4 the old and new forms agree exactly -- the fix is a no-op there,
    which is what makes it safe to deploy fleet-wide ahead of any cutover.
  * On 2026.x the old form fails or returns null and the new one returns real
    data -- that is the bug being fixed.

The queries are transcribed from cached.go.  The server rewrites ':Meta' and
':Neuron' to the dataset-prefixed labels, so they are sent verbatim.

Auth: NEUPRINT_JWT or NEUPRINT_TOKEN, sent in a header only, never printed.
Read-only.  Exit 0 if every dataset behaves as expected, 1 otherwise.
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.style import Style, add_color_argument, want_color  # noqa: E402

TIMEOUT = 300

# --- cypher2, getROIConnectivity_int: the overview ROI list ----------------
ROIS_OLD = """
    MATCH (meta :Meta)
    WITH
        CASE meta.overviewRois
            WHEN NULL THEN meta.primaryRois
            ELSE meta.overviewRois
        END AS overviewRois,
        meta,
        apoc.convert.fromJsonMap(meta.roiInfo) AS roiInfo
    UNWIND overviewRois as roi
    WITH roiInfo, roi
    WHERE NOT coalesce(roiInfo[roi]['excludeFromOverview'], FALSE)
    RETURN collect(roi) as rois
"""
ROIS_NEW = """
    MATCH (meta :Meta)
    WITH
        coalesce(meta.overviewRois, meta.primaryRois) AS overviewRois,
        meta,
        apoc.convert.fromJsonMap(meta.roiInfo) AS roiInfo
    UNWIND overviewRois as roi
    WITH roiInfo, roi
    WHERE NOT coalesce(roiInfo[roi]['excludeFromOverview'], FALSE)
    RETURN collect(roi) as rois
"""

# --- cypher3, getROIConnectivity_int: the overview ordering ----------------
ORDER_OLD = """
    MATCH (m:Meta)
    RETURN
        CASE m.overviewOrder
            WHEN NULL THEN 'clustered'
            ELSE m.overviewOrder
        END AS overviewOrder
"""
ORDER_NEW = """
    MATCH (m:Meta)
    RETURN coalesce(m.overviewOrder, 'clustered') AS overviewOrder
"""

# --- random_query, getDailyType_int: the eligible-type predicate -----------
# The shipped query ends in 'ORDER BY randvar LIMIT 1', which is by design not
# reproducible.  Counting the eligible types instead makes old and new
# comparable; the predicate under test is identical either way.
DAILY = """
    MATCH (n :Neuron)
    WHERE (n.cropped IS NULL OR not n.cropped) AND n.status IN ["Traced","Anchor"]
    WITH percentileDisc(n.pre, 0.2) AS prethres, percentileDisc(n.post, 0.2) AS postthres
    MATCH (n :Neuron)
    WHERE (n.cropped IS NULL OR not n.cropped) AND n.status IN ["Traced","Anchor"]
      AND PREDICATE AND n.type<>""
      AND (n.pre > prethres OR n.post > postthres)
    RETURN count(DISTINCT n.type) AS eligibleTypes
"""
DAILY_OLD = DAILY.replace('PREDICATE', 'EXISTS(n.type)')
DAILY_NEW = DAILY.replace('PREDICATE', 'n.type IS NOT NULL')


def cypher(server, token, dataset, q):
    req = urllib.request.Request(f'{server}/api/custom/custom')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', f'Bearer {token}')
    req.data = json.dumps({'dataset': dataset, 'cypher': q}).encode()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            data = json.loads(r.read()).get('data') or []
            return (data[0][0] if data else None), None
    except urllib.error.HTTPError as e:
        # neuPrintHTTP returns 400 for every Cypher error, so show the body.
        body = e.read().decode('utf-8', 'replace')[:120].replace('\n', ' ')
        return None, f'HTTP {e.code}: {body}'
    except Exception as e:
        return None, str(e)


def brief(v):
    if isinstance(v, list):
        return f'[{len(v)} rois]' if v else '[] EMPTY'
    return 'null' if v is None else repr(v)


def check(server, token, dataset, name, old_q, new_q, s):
    """Run both forms; print a line and return whether it behaved as expected."""
    old, old_err = cypher(server, token, dataset, old_q)
    new, new_err = cypher(server, token, dataset, new_q)

    if new_err:
        print(f'    {name:<14} {s.red("NEW FORM FAILED")}: {new_err}')
        return False
    empty_new = new is None or (isinstance(new, list) and not new)
    if empty_new:
        print(f'    {name:<14} {s.red("new form returned nothing")}: {brief(new)}')
        return False

    if old_err:
        print(f'    {name:<14} old: {s.yellow("ERROR")} ({old_err[:48]})   '
              f'new: {brief(new)}   -> {s.green("FIXED")}')
        return True
    empty_old = old is None or (isinstance(old, list) and not old)
    if empty_old:
        print(f'    {name:<14} old: {brief(old):<14} new: {brief(new):<14} '
              f'-> {s.green("FIXED")}')
        return True
    if old == new:
        print(f'    {name:<14} old: {brief(old):<14} new: {brief(new):<14} '
              f'-> {s.green("identical")}')
        return True
    print(f'    {name:<14} old: {brief(old):<14} new: {brief(new):<14} '
          f'-> {s.red("DIVERGED")}')
    return False


def main(argv):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('server')
    p.add_argument('datasets', nargs='*',
                   default=['hemibrain', 'vnc', 'wasp3:v0.8'])
    add_color_argument(p)
    if len(argv) < 2:
        print(__doc__)
        return 2
    args = p.parse_args(argv[1:])

    server = args.server
    if not server.startswith(('http://', 'https://')):
        server = (f'https://{server}.janelia.org' if '.' not in server
                  else f'https://{server}')
    datasets = args.datasets or ['hemibrain', 'vnc', 'wasp3:v0.8']
    token = os.environ.get('NEUPRINT_JWT') or os.environ.get('NEUPRINT_TOKEN')
    if not token:
        print('ERROR: set NEUPRINT_JWT (or NEUPRINT_TOKEN).', file=sys.stderr)
        return 2
    s = Style(want_color(args.color))

    print(f'server: {server}\n')
    ok = True
    for ds in datasets:
        ver, err = cypher(server, token, ds,
                          'CALL dbms.components() YIELD versions '
                          'RETURN versions[0]')
        print(f'{s.cyan(f"=== {ds}")}   (neo4j {err and "unknown" or ver})')
        ok &= check(server, token, ds, 'overviewRois', ROIS_OLD, ROIS_NEW, s)
        ok &= check(server, token, ds, 'overviewOrder', ORDER_OLD, ORDER_NEW, s)
        ok &= check(server, token, ds, 'dailyType', DAILY_OLD, DAILY_NEW, s)
        print()

    if ok:
        print(f'RESULT: {s.green("OK")} -- the new form works everywhere, and '
              'matches the old')
        print('form wherever the old form still works.  Safe to deploy '
              'fleet-wide.')
        return 0
    print(f'RESULT: {s.red("FAILED")} -- see DIVERGED or FAILED lines above.')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))

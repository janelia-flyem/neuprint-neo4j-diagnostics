#!/usr/bin/env python3
"""
cypher-version-diff.py <server> <dataset> [term ...]

Run neuPrintExplorer's actual FindNeurons queries under CYPHER 5 and CYPHER 25
on the same dataset and compare the results.

    ./cypher-version-diff.py neuprint-pre wasp3:v0.0

Why: a CalVer server's db.query.default_language defaults to CYPHER_25, while
the snapshot pipeline pins CYPHER_5 and the client's queries were written and
tested against 5. The fast query uses `CALL { WITH q ... }`, the importing-WITH
form that Cypher 25 deprecates in favor of a scope clause `CALL (q) { ... }`.
This answers whether that matters for the queries actually shipped, rather than
reasoning from a changelog.

Queries come from queries/*.cypher (see refresh-queries.sh), so what gets
compared is what the component sends.

Auth: NEUPRINT_JWT or NEUPRINT_TOKEN, sent in a header only.
Read-only. Exit 0 if the two versions agree everywhere, 1 if they diverge.
"""
import json
import argparse
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.style import Style, add_color_argument, want_color  # noqa: E402

HERE = Path(__file__).resolve().parent
# The extracted client queries are maintained in client/, by refresh-queries.sh.
QUERIES = HERE.parent / 'client' / 'queries'
DEFAULT_TERMS = ['a', 'LC(L)', 'AL-IN']
TIMEOUT = 300


def cypher(server, token, dataset, query):
    req = urllib.request.Request(f'{server}/api/custom/custom')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    req.data = json.dumps({'dataset': dataset, 'cypher': query}).encode()
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            return json.loads(r.read()).get('data') or [], time.time() - t0, None
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')[:200].replace('\n', ' ')
        return None, time.time() - t0, f'HTTP {e.code}: {body}'
    except Exception as e:
        return None, time.time() - t0, str(e)


def escape_cypher(v):
    return str(v).replace('\\', '\\\\').replace("'", "\\'")


def lucene_query(term):
    tokens = [t for t in re.split(r'[^a-z0-9_]+', str(term).lower()) if t]
    return ' AND '.join(f'*{t}*' for t in tokens) if tokens else '*'


def fill(template, term, body_id=0):
    return (template
            .replace('LUCENE', lucene_query(term))
            .replace('TERM', escape_cypher(term))
            .replace('BODY', str(body_id)))


def main(argv):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('server')
    p.add_argument('dataset')
    p.add_argument('terms', nargs='*',
                   help=f'search terms (default: {" ".join(DEFAULT_TERMS)})')
    add_color_argument(p)
    # Only a bare invocation prints the docstring; anything else goes to
    # argparse, so that --help reaches it and missing arguments get a usage
    # message rather than the whole manual.
    if len(argv) == 1:
        print(__doc__)
        return 2
    args = p.parse_args(argv[1:])

    server = args.server
    if not server.startswith(('http://', 'https://')):
        server = f'https://{server}.janelia.org' if '.' not in server else f'https://{server}'
    dataset = args.dataset
    terms = args.terms or DEFAULT_TERMS
    token = os.environ.get('NEUPRINT_JWT') or os.environ.get('NEUPRINT_TOKEN')
    if not token:
        print('ERROR: set NEUPRINT_JWT (or NEUPRINT_TOKEN).', file=sys.stderr)
        return 2
    s = Style(want_color(args.color))

    queries = {}
    for name in ('fast', 'slow'):
        p = QUERIES / f'{name}.cypher'
        if not p.is_file():
            print(f'ERROR: {p} missing -- run refresh-queries.sh', file=sys.stderr)
            return 2
        queries[name] = p.read_text()

    src = QUERIES / 'SOURCE'
    print(f'server:  {server}')
    print(f'dataset: {dataset}')
    print(f'queries: {src.read_text().strip() if src.is_file() else "unknown"}')
    print()

    diverged = False
    for name in ('fast', 'slow'):
        for term in terms:
            body = fill(queries[name], term)
            results = {}
            for ver in ('5', '25'):
                rows, secs, err = cypher(server, token, dataset,
                                         f'CYPHER {ver}\n{body}')
                results[ver] = (rows, secs, err)

            r5, t5, e5 = results['5']
            r25, t25, e25 = results['25']
            label = f'{name} / {term!r}'

            if e5 or e25:
                diverged = True
                print(f'  {label}')
                if e5:
                    print(f'      CYPHER 5  {s.red("ERROR")}  {e5}')
                if e25:
                    print(f'      CYPHER 25 {s.red("ERROR")}  {e25}')
                if e5 and not e25:
                    print(s.yellow('      -> fails on 5 but works on 25'))
                if e25 and not e5:
                    # The finding this script exists to catch.
                    print(s.red('      -> WORKS on 5 but FAILS on 25 '
                                '(pinning CYPHER_5 would be required)'))
                continue

            s5 = {r[0] for r in r5}
            s25 = {r[0] for r in r25}
            if s5 == s25:
                print(f'  {label:<28} {s.green("identical")}  ({len(s5)} rows)   '
                      f'5: {t5:.2f}s  25: {t25:.2f}s')
            else:
                diverged = True
                print(f'  {label:<28} {s.red("DIFFERS")}    5:{len(s5)} rows  25:{len(s25)} rows')
                only5, only25 = sorted(s5 - s25)[:5], sorted(s25 - s5)[:5]
                if only5:
                    print(f'      only under CYPHER 5:  {only5}')
                if only25:
                    print(f'      only under CYPHER 25: {only25}')

    print()
    if diverged:
        print(f'RESULT: {s.red("the two language versions do NOT agree")} -- see above.')
        return 1
    print(f'RESULT: {s.green("identical results under CYPHER 5 and CYPHER 25.")}')
    print('The pin is then a matter of stability, not current correctness:')
    print('nothing selected CYPHER_25, so it moves when Neo4j moves.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

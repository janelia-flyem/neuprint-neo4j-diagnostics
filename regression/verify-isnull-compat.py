#!/usr/bin/env python3
"""
verify-isnull-compat.py [server]

Check that the IS NULL / IS NOT NULL forms neuprint-python is being changed to
mean the same thing as the exists() forms they replace, on every Neo4j
generation in the fleet.

    ./verify-isnull-compat.py neuprint-pre

Why: `exists(variable.property)` was removed in Neo4j 5, so neuprint-python's
NeuronCriteria generates Cypher that a CalVer server rejects outright.  The
replacement has to work on the OLDEST server too, because one neuPrintHTTP
process serves Neo4j 3.5.3, 4.4.16 and 2026.08.1 at once and neuprint-python
talks to all of them.

Expected result:

  * hemibrain (3.5.3) and vnc (4.4.16): both spellings legal, counts identical
  * wasp3:v0.8 (2026.08.1):             old spelling rejected, new one works

Any MISMATCH means a substitution changes meaning rather than just syntax, and
the neuprint-python change should stop until it is understood.

Auth: NEUPRINT_JWT or NEUPRINT_TOKEN, sent in a header only, never printed.
Read-only.  Exit 0 if every substitution is equivalent, 1 otherwise.
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

# (old form as neuprint-python emits it today, replacement)
PAIRS = [
    ('(NOT n.cropped OR NOT exists(n.cropped))',
     '(NOT n.cropped OR n.cropped IS NULL)'),          # _tag_expr, line 979
    ('exists(n.somaLocation)',
     'n.somaLocation IS NOT NULL'),                    # _nullcheck_expr, 959
    ('NOT exists(n.somaLocation)',
     'n.somaLocation IS NULL'),                        # _nullcheck_expr, 957
    ("n.status in ['Traced'] OR NOT exists(n.status)",
     "n.status in ['Traced'] OR n.status IS NULL"),    # where_expr, 1328/1330
]
DATASETS = ['hemibrain', 'vnc', 'wasp3:v0.8']
TIMEOUT = 120


def count(server, token, dataset, where):
    req = urllib.request.Request(f'{server}/api/custom/custom', method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    req.data = json.dumps({
        'dataset': dataset,
        'cypher': f'MATCH (n:Neuron) WHERE {where} RETURN count(n) AS c',
    }).encode()
    try:
        with urllib.request.urlopen(
                req, timeout=TIMEOUT,
                context=ssl.create_default_context()) as r:
            return str(json.loads(r.read())['data'][0][0])
    except urllib.error.HTTPError as e:
        body = e.read()
        kind = 'syntax' if b'SyntaxError' in body else f'http{e.code}'
        return f'ERROR({kind})'
    except Exception as e:                              # noqa: BLE001
        return f'ERROR({type(e).__name__})'


def main(argv):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('server', nargs='?', default='neuprint-pre')
    add_color_argument(p)
    args = p.parse_args(argv[1:])

    server = args.server
    if not server.startswith(('http://', 'https://')):
        server = (f'https://{server}.janelia.org' if '.' not in server
                  else f'https://{server}')
    token = os.environ.get('NEUPRINT_JWT') or os.environ.get('NEUPRINT_TOKEN')
    if not token:
        print('ERROR: set NEUPRINT_JWT (or NEUPRINT_TOKEN).', file=sys.stderr)
        return 2
    s = Style(want_color(args.color))

    print(f'server: {server}')
    ok = True
    for ds in DATASETS:
        ver = count(server, token, ds, 'true')   # also proves the dataset is served
        reachable = not ver.startswith('ERROR')
        neo = s.green('ok') if reachable else s.red('unreachable')
        print(f'\n{s.cyan(f"=== {ds}")}   ({neo})')
        for old, new in PAIRS:
            o = count(server, token, ds, old)
            n = count(server, token, ds, new)
            if o == n:
                verdict = s.green('identical')
            elif o.startswith('ERROR(syntax)') and not n.startswith('ERROR'):
                verdict = s.green('old rejected -> new works')
            else:
                verdict = s.red('MISMATCH')
                ok = False
            # Color goes last: an escape sequence inside a width-formatted
            # field counts toward the width and breaks the columns.
            o_shown = s.red(f'{o:<16}') if o.startswith('ERROR') else f'{o:<16}'
            n_shown = s.red(f'{n:<16}') if n.startswith('ERROR') else f'{n:<16}'
            print(f'  {new:<46} old={o_shown} new={n_shown} {verdict}')

    print()
    if ok:
        print(f'RESULT: {s.green("OK")} -- every replacement means the same '
              'thing wherever the old form still runs,')
        print('and works where the old form is rejected. Safe on Neo4j 3.5, '
              '4.4 and 2026.x alike.')
        return 0
    print(f'RESULT: {s.red("MISMATCH")} -- a replacement changes meaning. Stop '
          'and investigate before changing neuprint-python.')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))

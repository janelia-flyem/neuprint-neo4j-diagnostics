#!/usr/bin/env python3
"""
check-neuprint-python.py <server> [dataset ...]

Run neuprint-python's REAL query builder against a live server and check that
what it generates still executes.

    ./check-neuprint-python.py neuprint-pre wasp3:v0.8
    ./check-neuprint-python.py neuprint-pre            # every visible dataset

Why this exists
---------------
`regression/verify-isnull-compat.py` checks that `x.y IS NULL` means the same
thing as `NOT exists(x.y)` on every Neo4j generation. That is a check on the
*substitution*, and its expressions are transcribed by hand. If NeuronCriteria
changes tomorrow, that script keeps passing while the real client breaks.

This one asks NeuronCriteria to generate the Cypher, then runs it. It would
have caught the `exists()` defect on the day a CalVer dataset appeared, which
nothing else here would: `exists(variable.property)` was removed in Neo4j 5,
and `NeuronCriteria(cropped=False)` emitted it, so ordinary analysis scripts
returned HTTP 400 against any migrated dataset.

Two things it looks for, per criteria shape:

  * **Generated text.** Any `exists(` in the output is flagged outright --
    that is the removed syntax, regardless of whether a 4.4 server accepts it.
  * **Execution.** The expression is run as a WHERE clause and the row count
    reported. A syntax error on one generation and not another is the
    signature of a removed construct.

Rows are counted, never fetched, so this is cheap even on a large dataset.

Requirements
------------
Unlike everything else in this repo, this needs `neuprint` importable -- the
point is to exercise the real code, not a copy of it. Run it under an
environment that has it:

    pixi run -e test python check-neuprint-python.py neuprint-pre
    PYTHONPATH=~/Documents/workspace/Git/neuprint-python ./check-neuprint-python.py neuprint-pre

Without it the script exits 3 and says so, rather than silently testing
nothing. `--neuprint-path` prepends a checkout to sys.path, but its
dependencies (pandas, pyarrow, requests) still have to be importable.

Auth: NEUPRINT_JWT or NEUPRINT_TOKEN. Note neuprint-python's own tests read
NEUPRINT_APPLICATION_CREDENTIALS instead; this accepts the repo's variable and
passes it to Client() explicitly, so one token works everywhere.

Read-only. Exit 0 if every shape generated valid, executable Cypher, 1 if any
did not, 2 on a bad invocation, 3 if neuprint could not be imported.
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
from common.style import Reporter, Style, add_color_argument, want_color  # noqa: E402

TIMEOUT = 300


def load_neuprint(extra_path=None):
    """Import neuprint, or explain why not. Returns the module namespace."""
    if extra_path:
        sys.path.insert(0, str(Path(extra_path).expanduser().resolve()))
    try:
        import neuprint
        from neuprint import (Client, NeuronCriteria, IsNull, NotNull,
                              set_default_client)
        from neuprint.queries.neuroncriteria import where_expr
        return dict(neuprint=neuprint, NC=NeuronCriteria, IsNull=IsNull,
                    NotNull=NotNull, where_expr=where_expr, Client=Client,
                    set_default_client=set_default_client)
    except ImportError as e:
        return str(e)


def criteria_cases(np):
    """Representative NeuronCriteria shapes.

    Chosen for the constructs they exercise rather than for coverage of the
    API: each one goes through a different branch of the expression builder,
    and the null-handling branches are the ones that broke.
    """
    NC, IsNull, NotNull = np['NC'], np['IsNull'], np['NotNull']
    return [
        # label,                        builder
        ('cropped=False',               lambda: NC(cropped=False)),
        ('cropped=True',                lambda: NC(cropped=True)),
        ('somaLocation=NotNull',        lambda: NC(somaLocation=NotNull)),
        ('somaLocation=IsNull',         lambda: NC(somaLocation=IsNull)),
        ('status=[Traced]',             lambda: NC(status=['Traced'])),
        ('status=[Traced, None]',       lambda: NC(status=['Traced', None])),
        ('status=None',                 lambda: NC(status=[None])),
        ('type=LC10',                   lambda: NC(type='LC10')),
        ('type=[LC10, LC11]',           lambda: NC(type=['LC10', 'LC11'])),
        ('type regex',                  lambda: NC(type='LC.*', regex=True)),
        ('bodyId=[1,2,3]',              lambda: NC(bodyId=[1, 2, 3])),
        ('instance=NotNull',            lambda: NC(instance=NotNull)),
    ]


def cypher_count(server, token, dataset, where):
    """Run one WHERE clause and return (count, error)."""
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
            data = json.loads(r.read()).get('data') or []
            return (data[0][0] if data else 0), None
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        kind = 'SyntaxError' if 'SyntaxError' in body else f'HTTP {e.code}'
        return None, kind
    except Exception as e:                                  # noqa: BLE001
        return None, type(e).__name__


def list_datasets(server, token):
    req = urllib.request.Request(f'{server}/api/dbmeta/datasets')
    req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(
                req, timeout=TIMEOUT,
                context=ssl.create_default_context()) as r:
            return sorted(json.loads(r.read()).keys())
    except Exception:                                       # noqa: BLE001
        return []


def neo4j_version(server, token, dataset):
    v, err = cypher_count(server, token, dataset, 'true')   # reachability
    if err:
        return 'unreachable'
    req = urllib.request.Request(f'{server}/api/custom/custom', method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    req.data = json.dumps({
        'dataset': dataset,
        'cypher': 'CALL dbms.components() YIELD versions RETURN versions[0]',
    }).encode()
    try:
        with urllib.request.urlopen(
                req, timeout=TIMEOUT,
                context=ssl.create_default_context()) as r:
            d = json.loads(r.read()).get('data') or []
            return d[0][0] if d else 'unknown'
    except Exception:                                       # noqa: BLE001
        return 'unknown'


def main(argv):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('server')
    p.add_argument('datasets', nargs='*')
    p.add_argument('--neuprint-path', metavar='DIR',
                   help='a neuprint-python checkout to import from')
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

    rep = Reporter(Style(want_color(args.color)))
    np = load_neuprint(args.neuprint_path)
    if isinstance(np, str):
        rep(rep.s.red('ERROR: neuprint could not be imported -- '
                      'nothing was tested.'))
        rep(f'  {np}')
        rep('')
        rep('  This check needs the real neuprint-python package, since the')
        rep('  point is to exercise its query builder rather than a copy.')
        rep('  Try one of:')
        rep('    pixi run -e test python client/check-neuprint-python.py ...')
        rep('    PYTHONPATH=<neuprint-python checkout> ./client/check-neuprint-python.py ...')
        return 3

    version = getattr(np['neuprint'], '__version__', 'unknown')
    rep(f'server:           {server}')
    rep(f'neuprint-python:  {version}  ({Path(np["neuprint"].__file__).parent})')

    datasets = args.datasets or list_datasets(server, token)
    if not datasets:
        rep(rep.s.red('ERROR: no datasets visible. Check the token.'))
        return 2

    cases = criteria_cases(np)

    # NeuronCriteria.basic_exprs() validates ROI names against a live client,
    # and does so even when no ROI criteria were given -- so a default Client
    # has to exist before a single expression can be generated. That is also
    # faithful: it is how a user's script reaches this code.
    try:
        client = np['Client'](server, datasets[0], token=token)
        np['set_default_client'](client)
    except Exception as e:                                  # noqa: BLE001
        rep(rep.s.red(f'ERROR: could not create a neuprint Client against '
                      f'{datasets[0]}: {type(e).__name__}: {e}'))
        return 2

    # --- 1. the generated text, which needs no further server calls --------
    rep.heading('--- generated Cypher ---')
    rep(rep.s.dim('  Any exists( ) here is the syntax Neo4j 5 removed, '
                  'whatever a 4.4 server accepts.'))
    exprs = {}
    text_ok = True
    for label, build in cases:
        try:
            parts = build().basic_exprs()
        except Exception as e:                              # noqa: BLE001
            rep(f'  {label:<24} {rep.s.red("builder raised")}: '
                f'{type(e).__name__}: {e}')
            rep.diff(f'generator / {label}: {type(e).__name__}')
            text_ok = False
            continue
        joined = ' AND '.join(f'({x})' for x in parts if x) or 'true'
        exprs[label] = joined
        if 'exists(' in joined.lower():
            rep(f'  {label:<24} {rep.s.red("USES exists()")}  {joined[:70]}')
            rep.diff(f'generator / {label}: emits removed exists() syntax')
            text_ok = False
        else:
            rep(f'  {label:<24} {rep.s.green("ok")}             '
                f'{rep.s.dim(joined[:70])}')

    # --- 2. execution, per dataset -----------------------------------------
    exec_ok = True
    for ds in datasets:
        ver = neo4j_version(server, token, ds)
        rep.heading(f'=== {ds}   (neo4j {ver})')
        if ver == 'unreachable':
            rep(f'  {rep.s.yellow("skipped")} -- dataset not reachable')
            rep.warn(f'{ds}: not reachable')
            continue
        for label, _ in cases:
            where = exprs.get(label)
            if where is None:
                continue
            n, err = cypher_count(server, token, ds, where)
            if err:
                rep(f'  {label:<24} {rep.s.red(err)}')
                rep.diff(f'{ds} / {label}: {err}')
                exec_ok = False
            else:
                rep(f'  {label:<24} {rep.s.green(f"{n:,} rows")}')

    rep.print_findings()
    ok = text_ok and exec_ok
    rep('')
    if ok:
        rep(f'RESULT: {rep.s.green("OK")} -- every NeuronCriteria shape '
            'generated valid Cypher that ran')
        rep('on every dataset checked.')
    else:
        rep(f'RESULT: {rep.s.red("FAILED")} -- see findings above. A '
            '"USES exists()" line means the')
        rep('installed neuprint-python will break against any Neo4j 5 or '
            'CalVer dataset.')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))

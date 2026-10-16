#!/usr/bin/env python3
"""
check-rollout.py <server> -- verify a neuPrintExplorer rollout on one server.

    ./check-rollout.py neuprint-pre
    ./check-rollout.py https://neuprint-pre.janelia.org
    ./check-rollout.py neuprint-pre lc a KCab-s     # specific search terms

Options:

    --color auto|always|never
        When to colorize PASS/FAIL/WARN.  "auto" (the default) is on for a
        terminal and off when piped, so escape codes never land in a file.
        "always" forces it on even through a pipe -- use it when the reader
        can render color, such as `| less -R`.  "never" forces it off.
        Without the flag, the COLOR environment variable is consulted (the
        shell scripts in this repo use that), then NO_COLOR.  The saved
        report in results/ is always plain text regardless.

        Parsed by hand rather than by argparse, because the trailing
        arguments here are search terms and argparse would claim them.

Two independent things are checked, because they can fail separately:

  1. Is the new client actually deployed?  The served bundle is fetched and
     grepped for the fixes.  Everything else here probes the SERVER, which
     looks identical whether the new client is live or not -- so without this,
     a clean run says nothing about whether the rollout arrived.

  2. Does the server answer the client's queries correctly?  For every dataset,
     the FindNeurons capability decision is replayed, whichever query that
     implies is run, and the result is compared against buildSlowQuery as
     ground truth.

Exit codes:
  0  OK -- new client live, server answers it correctly
  1  FAILED (server checks) -- do not roll out here until resolved
  3  NOT DEPLOYED -- server is ready, the new bundle just is not live yet
  2  could not complete the run (no token, bad invocation, server unreachable)

1 and 3 are split because before a rollout the deployment check is *expected*
to fail, and that must not look the same as a server that mishandles the
client's queries.

Auth: set NEUPRINT_JWT (or NEUPRINT_TOKEN).  /api/dbmeta/datasets returns
FEWER datasets to an anonymous client, so an unauthenticated run can produce a
confident all-clear that means nothing.  This script refuses to treat that as
a pass.  The token is sent in a header only -- never logged or written out.

Results are written to results/<server>-<timestamp>.txt (human) and .tsv
(one row per dataset/term, for diffing across machines).
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent                       # the diagnostics repo root

sys.path.insert(0, str(REPO))
from common.style import Style, strip_ansi, want_color  # noqa: E402
QUERIES = HERE / 'queries'
RESULTS = REPO / 'results'               # one results/ dir shared by every tool

# The eleven properties buildSlowQuery scans and buildFastQuery ranks on.
SEARCH_PROPS = [
    'type', 'instance', 'hemibrainType', 'flywireType', 'systematicType',
    'itoleeHl', 'trumanHl', 'synonyms', 'class', 'entryNerve', 'exitNerve',
]

# Terms chosen for what this rollout changed. The punctuation cases are the
# ones that returned zero rows before #386: the analyzer splits on punctuation,
# so no token ever equals 'r1-r6'.  A term legitimately matching nothing on a
# given dataset is fine -- the assertion is fast-vs-slow agreement, not a
# minimum row count, so the same terms are meaningful on every dataset.
DEFAULT_TERMS = ['a', 'R1-R6', 'KCab-s', 'SNta02,SNta09', "Tm5'", 'LC(L)']

# Markers proving the deployed bundle carries each fix. Verified 2026-09-30 to
# be present on neuprint-test (new client) and absent on neuprint-pre (old).
BUNDLE_MARKERS = [
    ('SHOW INDEXES',   True,  'Neo4j 5 capability probe (#383)'),
    ('db.indexes()',   False, 'removed-in-5.0 call must be gone (#383)'),
    ('[^a-z0-9_]+',    True,  'buildLuceneQuery tokenizer (#386)'),
]

TIMEOUT = 300


class Reporter:
    """Collects lines for the report while echoing them to the terminal."""

    def __init__(self, style=None):
        self.lines = []
        # Shared palette, so a PASS here looks like an "identical" in
        # compare-datasets.py. See common/style.py.
        self.s = style or Style(False)
        self.passes = self.warnings = 0
        # How many real fast-vs-slow comparisons actually ran. Zero means
        # nothing was verified, however many datasets were visited -- a
        # server whose datasets all skip must not be reported as checked.
        self.comparisons = 0
        # Kept apart on purpose. "The new client is not here yet" and "the
        # server mishandles the client's queries" are different answers that
        # want different actions, and before a rollout the first is expected.
        self.server_failures = 0
        self.deploy_failures = 0
        self.warning_lines = []

    @property
    def failures(self):
        return self.server_failures + self.deploy_failures

    def _emit(self, text):
        # The report file keeps the plain text; the terminal gets the color.
        self.lines.append(strip_ansi(text))
        print(text, flush=True)

    def head(self, text):
        self._emit('')
        self._emit(self.s.cyan('=' * 62))
        self._emit(self.s.cyan(f' {text}'))
        self._emit(self.s.cyan('=' * 62))

    def ok(self, msg):
        self.passes += 1
        self._emit(f'  {self.s.green("PASS")}  {msg}')

    def bad(self, msg):
        self.server_failures += 1
        self._emit(f'  {self.s.red("FAIL")}  {msg}')

    def bad_deploy(self, msg):
        self.deploy_failures += 1
        self._emit(f'  {self.s.red("FAIL")}  {msg}')

    def warn(self, msg):
        self.warnings += 1
        self.warning_lines.append(msg)
        self._emit(f'  {self.s.yellow("WARN")}  {msg}')

    def skip(self, msg):
        self._emit(f'  {self.s.dim("SKIP")}  {msg}')

    def info(self, msg):
        self._emit(f'  {self.s.dim("....")}  {msg}')


def normalize_server(arg):
    """Accept 'neuprint-pre' or a full URL."""
    if arg.startswith(('http://', 'https://')):
        return arg.rstrip('/')
    if '.' not in arg:
        return f'https://{arg}.janelia.org'
    return f'https://{arg}'


def server_label(server):
    """A filename-safe short name, e.g. https://neuprint-pre.janelia.org -> neuprint-pre."""
    host = re.sub(r'^https?://', '', server).split('/')[0]
    return host.split('.')[0] or re.sub(r'[^A-Za-z0-9_.-]', '_', host)


def fetch(url, token=None, data=None, timeout=TIMEOUT):
    """Return (status, body_bytes). Never raises for HTTP status."""
    req = urllib.request.Request(url)
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(data).encode()
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                       # DNS, TLS, timeout, reset
        return 0, str(e).encode()


def cypher(server, token, dataset, query):
    """Run Cypher via /api/custom/custom.

    neuPrintHTTP returns HTTP 400 for EVERY CypherRequest error including a
    lost Bolt connection, and a 401 arrives as {"message": ...} with no
    "error" key -- which would otherwise read as an empty result set and be
    reported as 'nothing lost'.  So any non-200 becomes an explicit error.
    """
    status, body = fetch(f'{server}/api/custom/custom', token,
                         {'dataset': dataset, 'cypher': query})
    if status != 200:
        snippet = body.decode('utf-8', 'replace')[:200].replace('\n', ' ')
        return {'error': f'HTTP {status}: {snippet}'}
    try:
        return json.loads(body)
    except Exception as e:
        return {'error': f'unparseable response: {e}'}


def escape_cypher(value):
    """Mirror the client's escapeForCypher."""
    return str(value).replace('\\', '\\\\').replace("'", "\\'")


def lucene_query(term):
    """Mirror the client's buildLuceneQuery (v1.72.3+)."""
    tokens = [t for t in re.split(r'[^a-z0-9_]+', str(term).lower()) if t]
    if not tokens:
        return '*'
    return ' AND '.join(f'*{t}*' for t in tokens)


def fill(template, term, body_id=0):
    """Substitute the placeholders extract-queries.sh leaves behind.

    Done with str.replace rather than sed because terms contain commas,
    apostrophes and parentheses, and TERM appears more than once.
    """
    return (template
            .replace('LUCENE', lucene_query(term))
            .replace('TERM', escape_cypher(term))
            .replace('BODY', str(body_id)))


def load_queries():
    needed = ['fast', 'slow', 'version', 'index']
    missing = [n for n in needed if not (QUERIES / f'{n}.cypher').is_file()]
    if missing:
        sys.exit(f'ERROR: queries/{{{",".join(missing)}}}.cypher missing -- '
                 'run refresh-queries.sh first')
    return {n: (QUERIES / f'{n}.cypher').read_text() for n in needed}


PAGE_ATTEMPTS = 4
PAGE_RETRY_DELAY = 5


def fetch_page_with_bundle(server):
    """Fetch index.html, retrying until it carries a bundle reference.

    A server mid-restart serves a short placeholder -- observed on
    neuprint-pre as a 200 with a 157-byte body and no script tag. Taking that
    at face value reports 'no bundle reference', which reads as a broken
    deployment rather than a server that is not up yet. Since a restart is
    exactly when someone runs this, retry before concluding anything.

    Returns (match, status, attempts, last_size).
    """
    status = size = 0
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        status, body = fetch(f'{server}/', timeout=60)
        if status == 200:
            text = body.decode('utf-8', 'replace')
            size = len(body)
            m = re.search(r'/js/app\.([a-f0-9]+)\.bundle\.js', text)
            if m:
                return m, status, attempt, size
        if attempt < PAGE_ATTEMPTS:
            time.sleep(PAGE_RETRY_DELAY)
    return None, status, PAGE_ATTEMPTS, size


def check_bundle(server, rep):
    """Confirm the served bundle carries the fixes. Returns True if it does."""
    rep.head('Deployed client')
    m, status, attempts, size = fetch_page_with_bundle(server)
    if attempts > 1:
        rep.info(f'index.html took {attempts} attempts '
                 f'({PAGE_RETRY_DELAY}s apart) -- server may be restarting')
    if m is None:
        if status != 200:
            rep.bad_deploy(f'could not fetch {server}/ (HTTP {status}) after '
                           f'{attempts} attempts')
        else:
            rep.bad_deploy(f'no /js/app.<hash>.bundle.js reference in '
                           f'index.html after {attempts} attempts '
                           f'(last body {size} bytes)')
            rep.info('either the server is still coming up, or the page layout')
            rep.info('changed and this check needs updating')
        return False
    path, digest = m.group(0), m.group(1)
    rep.info(f'bundle: app.{digest}.bundle.js')
    status, js = fetch(f'{server}{path}', timeout=120)
    if status != 200:
        rep.bad_deploy(f'could not fetch the bundle (HTTP {status})')
        return False
    text = js.decode('utf-8', 'replace')
    rep.info(f'size: {len(js):,} bytes')
    all_good = True
    for marker, want_present, why in BUNDLE_MARKERS:
        present = marker in text
        if present == want_present:
            rep.ok(f'{why}')
        else:
            all_good = False
            state = 'missing' if want_present else 'still present'
            rep.bad_deploy(f'{why} -- marker {marker!r} {state}')
    if not all_good:
        rep.info('the old client appears to be deployed; server results below')
        rep.info('describe this server, not the rollout')
    return all_good


def list_datasets(server, token):
    status, body = fetch(f'{server}/api/dbmeta/datasets', token, timeout=60)
    if status != 200:
        return None, f'HTTP {status}'
    try:
        d = json.loads(body)
    except Exception as e:
        return None, f'unparseable: {e}'
    if not isinstance(d, dict) or d.get('error') or d.get('message') or not d:
        return None, 'no datasets returned (not authorized?)'
    return sorted(d), None


def probe_dataset(server, token, dataset, q):
    """Replay the client's capability decision. Returns a dict describing it."""
    out = {'version': None, 'state': None, 'decision': 'slow', 'reason': ''}

    v = cypher(server, token, dataset, q['version'])
    rows = v.get('data') or []
    if v.get('error') or not rows:
        out['reason'] = 'version probe failed -> client catches and uses slow query'
        return out
    out['version'] = rows[0][0]
    try:
        major_minor = float('.'.join(str(out['version']).split('.')[:2]))
    except Exception:
        major_minor = 0.0
    if major_minor < 4.4:
        out['reason'] = f'Neo4j {out["version"]} has no CALL {{}} subqueries'
        return out

    i = cypher(server, token, dataset, q['index'])
    rows = i.get('data') or []
    if i.get('error') or not rows:
        out['reason'] = 'no find_neurons_fulltext_properties_index'
        return out
    out['state'] = rows[0][0]
    if out['state'] != 'ONLINE':
        out['reason'] = f'index is {out["state"]}, not ONLINE'
        return out

    out['decision'] = 'fast'
    return out


def index_coverage(server, token, dataset):
    """Which searched properties are populated but not indexed.

    A gap is a config choice in flyem-snapshot -- the default indexes
    type/instance/synonyms and a dataset that annotates more extends the list
    in its own snapshot config.  It matters here only because it EXPLAINS row
    loss that would otherwise look like a client regression.
    """
    props = cypher(server, token, dataset,
                   'SHOW INDEXES YIELD name, type, properties '
                   "WHERE type = 'FULLTEXT' RETURN properties")
    rows = props.get('data') or []
    indexed = set(rows[0][0] or []) if rows and not props.get('error') else set()

    counts = ', '.join(f'count(n.`{p}`)' for p in SEARCH_PROPS)
    pop = cypher(server, token, dataset, f'MATCH (n:Neuron) RETURN {counts}')
    rows = pop.get('data') or []
    if pop.get('error') or not rows:
        return indexed, None, 'could not count populated properties'
    populated = {p for p, c in zip(SEARCH_PROPS, rows[0]) if c}
    return indexed, sorted(populated - indexed), None


def discover_punct_term(server, token, dataset):
    """Find a real annotation value on this dataset containing punctuation.

    The fixed term list is guesswork across a fleet -- 'R1-R6' means nothing
    on wasp3 -- and a term matching zero rows tests nothing. Asking the
    dataset for one of its own punctuated values guarantees the tokenizer
    path is actually exercised, whatever the dataset is.

    A substring of the value is used, not the whole thing: an exact value
    matches itself and little else, whereas a fragment spanning the
    punctuation is what used to return zero rows.
    """
    for prop in ('type', 'instance'):
        r = cypher(server, token, dataset,
                   f'MATCH (n:Neuron) WHERE n.`{prop}` IS NOT NULL '
                   f"AND n.`{prop}` =~ '.*[^A-Za-z0-9_ ].*' "
                   f'RETURN n.`{prop}` LIMIT 1')
        rows = r.get('data') or []
        if r.get('error') or not rows or not rows[0][0]:
            continue
        value = str(rows[0][0])
        # Trim to a fragment that still straddles a punctuation character,
        # so the analyzer has to split it into two tokens.
        m = re.search(r'[A-Za-z0-9]{1,6}[^A-Za-z0-9_ ][A-Za-z0-9]{1,6}', value)
        return m.group(0) if m else value
    return None


def bodies(result):
    """Row identities as a set, or None if the query errored."""
    if result.get('error'):
        return None
    return {r[0] for r in (result.get('data') or [])}


def main(argv):
    if len(argv) < 2 or argv[1] in ('-h', '--help'):
        print(__doc__)
        return 0 if len(argv) > 1 else 2

    # Trailing arguments are search terms, so --color is pulled out by hand
    # rather than by argparse, which would claim them as its own.
    color, rest = None, []          # None = flag absent, see want_color
    i = 1
    while i < len(argv):
        if argv[i].startswith('--color='):
            color = argv[i].split('=', 1)[1]
        elif argv[i] == '--color' and i + 1 < len(argv):
            i += 1
            color = argv[i]
        else:
            rest.append(argv[i])
        i += 1
    if color is not None and color not in ('auto', 'always', 'never'):
        print(f"ERROR: --color must be auto, always or never (got {color!r}).",
              file=sys.stderr)
        return 2
    argv = [argv[0]] + rest

    server = normalize_server(argv[1])
    label = server_label(server)
    terms = argv[2:] or DEFAULT_TERMS
    token = os.environ.get('NEUPRINT_JWT') or os.environ.get('NEUPRINT_TOKEN')
    q = load_queries()

    rep = Reporter(Style(want_color(color)))
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    started = time.time()

    rep._emit(f'neuPrintExplorer rollout check')
    rep._emit(f'server:  {server}')
    rep._emit(f'started: {datetime.now().isoformat(timespec="seconds")}')
    rep._emit(f'terms:   {", ".join(terms)}')
    rep._emit(f'auth:    {"bearer token supplied" if token else "NONE"}')

    # Which client's queries are being sent matters as much as which one is
    # deployed, and the two need not agree: run this before a rollout and the
    # bundle check fails while the query checks pass, which is exactly the
    # useful answer -- "not deployed yet, and it will work when it is".
    src = QUERIES / 'SOURCE'
    rep._emit(f'queries: {src.read_text().strip() if src.is_file() else "unknown provenance -- run refresh-queries.sh"}')

    # The bundle check needs no credentials, so run it before the auth gate:
    # without a token you still learn whether the rollout actually landed,
    # which is the question a deploy most often needs answered.
    check_bundle(server, rep)

    # Neither of the next two is a verdict on the server -- they mean the run
    # could not be completed -- so both exit 2 rather than counting as a
    # server failure, which would read as "this server is broken".
    if not token:
        rep.head('Authentication')
        rep._emit('  ABORT no NEUPRINT_JWT/NEUPRINT_TOKEN set -- server checks skipped')
        rep.info('/api/dbmeta/datasets hides datasets from anonymous clients')
        rep.info('(nine vs ten on neuprint.janelia.org, and the hidden one was')
        rep.info('the worst case we found), so an unauthenticated run cannot')
        rep.info('clear a server. The deployed-client result above still holds.')
        return write_reports(rep, [], label, stamp, server, started, force_exit=2)

    datasets, err = list_datasets(server, token)
    if datasets is None:
        rep.head('Datasets')
        rep._emit(f'  ABORT could not list datasets: {err}')
        return write_reports(rep, [], label, stamp, server, started, force_exit=2)

    rep.head('Datasets')
    rep.info(f'{len(datasets)} visible: {", ".join(datasets)}')

    tsv_rows = []
    for ds in datasets:
        rep.head(f'{ds}')
        p = probe_dataset(server, token, ds, q)
        rep.info(f'Neo4j {p["version"] or "unknown"}, '
                 f'index {p["state"] or "absent"} -> '
                 f'client uses the {p["decision"]} query')

        if p['decision'] == 'slow':
            rep.skip(f'fast path not in use ({p["reason"]})')
            rep.info('buildSlowQuery is a label scan and always complete, so')
            rep.info('there is no fast-vs-slow divergence to test here')
            tsv_rows.append([ds, p['version'] or '', p['state'] or 'absent',
                             'slow', '', '', '', '', 'SKIP'])
            continue

        indexed, gap, gap_err = index_coverage(server, token, ds)
        if gap_err:
            rep.warn(f'{ds}: {gap_err}')
            gap = []
        rep.info(f'index covers {len(indexed)} of {len(SEARCH_PROPS)} '
                 f'searched properties')
        if gap:
            rep.info(f'populated but unindexed: {", ".join(gap)}')

        ds_terms = list(terms)
        found = discover_punct_term(server, token, ds)
        if found and found not in ds_terms:
            ds_terms.append(found)
            rep.info(f"punctuated fragment taken from this dataset: '{found}' "
                     f'-> lucene {lucene_query(found)}')
        elif not found:
            rep.info('no punctuated annotation values found here')

        compared = 0
        for term in ds_terms:
            fast = cypher(server, token, ds, fill(q['fast'], term))
            slow = cypher(server, token, ds, fill(q['slow'], term))
            f_set, s_set = bodies(fast), bodies(slow)

            if f_set is None or s_set is None:
                which = 'fast' if f_set is None else 'slow'
                detail = (fast if f_set is None else slow)['error']
                rep.bad(f"{ds} / '{term}': the {which} query errored -- {detail}")
                tsv_rows.append([ds, p['version'], p['state'], 'fast', term,
                                 '', '', '', 'FAIL(query error)'])
                continue

            # A term matching nothing on this dataset proves nothing: 'fast
            # lost no rows' is trivially true when there were none to lose.
            # Counting that as a PASS inflates the verdict and hides the fact
            # that the term set does not exercise this dataset at all.
            if not s_set:
                rep.info(f"{ds} / '{term}': no rows match on this dataset "
                         '-- nothing to compare')
                tsv_rows.append([ds, p['version'], p['state'], 'fast', term,
                                 len(f_set), 0, 0, 'NO-COMPARISON'])
                continue

            compared += 1
            rep.comparisons += 1
            lost = len(s_set - f_set)
            pct = 100.0 * lost / len(s_set)

            if lost == 0:
                rep.ok(f"{ds} / '{term}': fast returns every row the slow "
                       f'query does ({len(s_set)})')
                verdict = 'PASS'
            elif gap:
                # Predicted consequence of the coverage gap, not a regression:
                # the client trusts the index by design since #387.
                rep.warn(f"{ds} / '{term}': {lost} of {len(s_set)} rows "
                         f'({pct:.1f}%) not returned by the fast query; '
                         f'explained by the unindexed properties above')
                verdict = 'WARN(index gap)'
            else:
                rep.bad(f"{ds} / '{term}': {lost} of {len(s_set)} rows "
                        f'({pct:.1f}%) lost with NO index gap to explain it')
                verdict = 'FAIL(unexplained loss)'

            tsv_rows.append([ds, p['version'], p['state'], 'fast', term,
                             len(f_set), len(s_set), lost, verdict])

        if compared == 0:
            rep.warn(f'{ds}: none of the terms matched anything here, so the '
                     'fast path was never actually exercised')
            rep.info('pass a term that exists in this dataset, e.g. from')
            rep.info("  MATCH (n:Neuron) WHERE n.type IS NOT NULL")
            rep.info('  RETURN n.type LIMIT 5')

    return write_reports(rep, tsv_rows, label, stamp, server, started)


def write_reports(rep, tsv_rows, label, stamp, server, started, force_exit=None):
    rep.head('Summary')
    rep._emit(f'  passed:            {rep.passes}')
    rep._emit(f'  comparisons run:   {rep.comparisons}')
    rep._emit(f'  server failures:   {rep.server_failures}')
    rep._emit(f'  deployment checks: '
              f'{"FAILED" if rep.deploy_failures else "passed"}'
              f'{f" ({rep.deploy_failures})" if rep.deploy_failures else ""}')
    if rep.warnings:
        rep._emit(f'  warnings:          {rep.warnings}')
        for w in rep.warning_lines:
            rep._emit(f'    {w}')
    rep._emit(f'  elapsed:           {time.time() - started:.0f}s')

    # A server problem outranks everything: if the server mishandles the
    # client's queries, deploying the client there is wrong whether or not it
    # has already happened. Only once the server is clean does "is it
    # deployed?" become the headline.
    if force_exit is not None:
        # The run stopped early, so there is no verdict on the server to give.
        # Without this the zero failure counters would print "OK".
        verdict, code = 'INCOMPLETE', force_exit
        note = ('The server checks did not run -- see above. Any deployment '
                'result reported above is still valid.')
    elif rep.server_failures:
        verdict, code = 'FAILED (server checks)', 1
        note = 'Do not roll out to this server until these are resolved.'
    elif rep.comparisons == 0:
        # No dataset here reaches the fast path, so nothing was compared and
        # "the server answers the client correctly" would be an empty claim.
        # Not a failure: the fixes concern the fulltext path, and a server
        # without one cannot be broken by them. But say what was and was not
        # established rather than implying a clean bill of health.
        deployed = 'not yet live' if rep.deploy_failures else 'live'
        verdict = f'NOTHING TO VERIFY (new client {deployed})'
        code = 3 if rep.deploy_failures else 0
        note = ('No dataset on this server reaches the fulltext fast path, so '
                'no\nfast-vs-slow comparison ran and nothing about query '
                'behavior was\nestablished either way. The changes only affect '
                'that path, so the\nrollout carries little risk here -- but '
                'this is not a verified pass.')
    elif rep.deploy_failures:
        verdict, code = 'NOT DEPLOYED (server checks passed)', 3
        note = (f'This server is ready: {rep.comparisons} comparison(s) ran and '
                'the server answers\nthe new client\'s queries correctly. The '
                'new bundle is simply not live\nhere yet. Re-run after '
                'deploying.')
    else:
        verdict, code = 'OK', 0
        note = (f'New client is live and the server answers it correctly '
                f'({rep.comparisons} comparison(s)).')

    rep._emit('')
    rep._emit(f'RESULT: {verdict}')
    for line in note.split('\n'):
        rep._emit(f'  {line}')

    RESULTS.mkdir(exist_ok=True)
    txt = RESULTS / f'{label}-{stamp}.txt'
    txt.write_text('\n'.join(rep.lines) + '\n')

    tsv = RESULTS / f'{label}-{stamp}.tsv'
    with tsv.open('w') as f:
        f.write('\t'.join(['dataset', 'neo4j', 'index_state', 'query', 'term',
                           'fast_rows', 'slow_rows', 'lost', 'verdict']) + '\n')
        for row in tsv_rows:
            f.write('\t'.join(str(c) for c in row) + '\n')

    print(f'\nwrote {txt}')
    print(f'wrote {tsv}')
    return code if force_exit is None else force_exit


if __name__ == '__main__':
    sys.exit(main(sys.argv))

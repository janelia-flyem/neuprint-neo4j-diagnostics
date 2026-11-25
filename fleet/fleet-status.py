#!/usr/bin/env python3
"""
fleet-status.py [options]

One picture of every neuPrint dataset across the swarm: which neo4j services
exist, where they run, what version they are, which neuprinthttp instances
point at them, what those instances actually serve, and which dataset:tag
each running neo4j holds. Then the places where those disagree.

    ./fleet-status.py                           # full run, report + CSV
    ./fleet-status.py --no-meta                 # skip the cypher-shell pass
    ./fleet-status.py --no-dvid                 # skip asking DVID about each uuid
    ./fleet-status.py --notes-from old-map.csv  # carry the Notes column over

Why this exists
---------------
The map of "which neo4j service is behind which neuprinthttp config" was a
spreadsheet filled in by hand from `docker service ls`, the swarm stack file
and ten config.json files on three hosts. It was right on the day it was
made. This script rebuilds it from the live swarm in one run and adds the two
things a spreadsheet cannot hold: what each instance serves right now, and
which tag each database carries -- the only way to tell wasp3:v0.8 from
wasp3:v0.0, whose uuid and lastDatabaseEdit are identical.

Where the facts come from, and how far each one is trusted
----------------------------------------------------------
  swarm      `docker service ls / inspect / ps` and `docker node ls` on the
             manager. Replicas (1/1 Up, 0/0 Down), image tag (the Neo4j
             version), placement constraint (where it should run) and the
             node its task is on (where it does run).
  configs    /opt/conf/<instance>/conf/config.json, read from the node each
             neuprinthttp task runs on -- not from the manager, because every
             host carries copies and some are stale. The main store is the
             spreadsheet's M; each mainstore-alternatives entry is an A.
  endpoints  https://neuprint-<x>.janelia.org/api/{version,dbmeta/database,
             dbmeta/datasets}. What the instance serves, from outside.
  meta       MATCH (m:Meta) RETURN m.dataset, m.tag, m.uuid,
             m.latestMutationId, m.lastDatabaseEdit, ... on each running
             neo4j, via cypher-shell in a throwaway container on the swarm
             overlay network, so it works for services on nodes where this
             account has no docker access. 3.5 servers have auth enabled;
             their credentials are taken from the config.json that points at
             them, held in memory, and never written anywhere.
  dvid       GET /api/repo/<uuid>/info on the DVID server each config names
             as the skeleton source for that dataset:tag. Says whether the
             node a store was built from is locked, how many mutations the
             repo has seen since, and which node the skeletons come from.
             No DVID token is used; repo info is readable on the site network.

Each source is collected independently. When one cannot be read the report
says so and the others still run; a verdict that needs a missing source is
skipped, not guessed.

What the verdicts mean
----------------------
  DIFF  something is inconsistent between two sources and should be looked
        at: a config points at a service that is Down or does not exist; a
        service is Up but no config references it; a service is Up but
        nothing answers on bolt, or a one-row Meta query does not finish;
        a dataset:tag is held by two running services; a placement
        constraint names one node and the task runs on another; an endpoint
        lists a dataset that no store behind it can explain, or omits one
        that a store behind it holds; a tagged dataset (a release) was built
        from a DVID node that is not locked, so the tag names data that can
        still change under it.
  WARN  worth knowing, not a fault: an endpoint unreachable from here, a
        config directory with no service (Not deployed), a stale config copy
        on a node that runs no neuprinthttp, a config.json that could not be
        read, a probe node judged unsafe, a store with no VIP, a Meta read
        that failed for a reason other than bolt being down, a DVID server
        that could not be asked, two stores that share a uuid but saw
        different mutation ids (an unlocked node read at different times,
        so "same uuid" proves nothing), skeletons fetched from a DVID node
        upstream of the store's or outside its lineage.

Outputs
-------
  results/fleet-<stamp>.txt   the report, plain text
  results/fleet-<stamp>.csv   the spreadsheet: the original's columns
        (neo4j service, Status, neo4j version, node, one M/A column per
        neuprinthttp instance, notes) plus running on, since, serves,
        served by, uuid, dvid node, mutation id, captured, pre / post,
        data dir, heap, page cache and memory limit.
  Pass --notes-from <csv> to copy the notes column from a previous CSV by
  service name, so hand-written context survives a regeneration.

Requirements and safety
-----------------------
Python 3 stdlib only. ssh as --user to --manager, to --probe-node and to
each node that runs a neuprinthttp instance (defaults: the DEFAULT_*
constants below); keys must already be in place (BatchMode, no
prompts). NEUPRINT_JWT or NEUPRINT_TOKEN for the endpoints; plain HTTP to the
DVID servers the configs name, with no token. Read-only against
the swarm: nothing is scaled, started, stopped or written on any host. The one
thing it creates is one short-lived `docker run --rm` container on the probe
node per run, and only after checking that node's kernel log and free memory
say a container start is safe there.
Passwords read from config.json are used for cypher-shell only and appear
in no output. Exit 0 if no DIFF, 1 if any, 2 if the run could not complete.
"""
import argparse
import concurrent.futures
import csv
import json
import os
import re
import shlex
import subprocess
import sys
import shutil
import socket
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.style import (Reporter, Style, add_baseline_argument,  # noqa: E402
                          add_color_argument, load_baseline, want_color)

STACK = 'em_services'
DEFAULT_MANAGER = 'emdata7'     # swarm manager: read-only docker queries only
DEFAULT_PROBE_NODE = 'emdata5'  # idle worker: the one container per run starts here
DEFAULT_USER = 'flyem'          # operator account with ssh to every node
NEO4J_PREFIX = f'{STACK}_neo4j-'
HTTP_PREFIX = f'{STACK}_neuprinthttp'
SSH_TIMEOUT = 60
CYPHER_TIMEOUT = 120
HTTP_TIMEOUT = 30
DVID_TIMEOUT = 30
# One pipe-joined string per Meta node, so a field with a comma in it could
# never break cypher-shell's comma-separated output.
META_FIELDS = ['dataset', 'tag', 'uuid', 'hideDataSet', 'totalPreCount', 'totalPostCount',
               'latestMutationId', 'lastDatabaseEdit']
META_CYPHER = ('MATCH (m:Meta) RETURN '
               + " + '|' + ".join(f"coalesce(toString(m.{f}), '')" for f in META_FIELDS)
               + ' AS row')

# The spreadsheet's column order for the neuprinthttp instances. Anything the
# swarm has that is not listed here is appended after these, alphabetically.
INSTANCE_ORDER = ['public', 'test', 'test-bak', 'pre', 'cns', 'examples',
                  'yakuba', 'zipursky', 'fish2', 'fabg']


# ---- remote plumbing --------------------------------------------------------

class Remote:
    """ssh to one account on the fleet. Every call is non-interactive; a host
    that would prompt fails fast instead of hanging.

    One connection per host is opened on first use and shared by every later
    command (ssh ControlMaster). That is what makes the Meta pass tolerable
    (28 commands, one handshake) and it is also what makes a run survive a
    resolver hiccup: the name is looked up once, when the master connects.
    """

    def __init__(self, user, rep):
        self.user = user
        self.rep = rep
        self.hosts = set()
        self.sockdir = self._socket_dir()
        self.opts = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                     '-o', 'StrictHostKeyChecking=accept-new', '-o', 'LogLevel=ERROR',
                     '-o', 'ControlMaster=auto', '-o', 'ControlPersist=600',
                     '-o', f'ControlPath={self.sockdir}/%r@%h:%p']

    @staticmethod
    def _socket_dir():
        # Unix socket paths are capped at ~104 bytes on macOS; $TMPDIR there
        # is already ~50. Fall back to /tmp when the default would not fit.
        d = tempfile.mkdtemp(prefix='fleet-ssh-')
        if len(d) + len('/operator@some-long-hostname.example.org:22') > 100:
            os.rmdir(d)
            d = tempfile.mkdtemp(prefix='fleet-ssh-', dir='/tmp')
        return d

    def ssh(self, host, script, timeout=SSH_TIMEOUT):
        """Run a shell script on host. Returns (rc, stdout, stderr).
        rc 255 is ssh itself failing (resolution, connection, auth) rather
        than the script; one retry covers a momentary resolver blip."""
        cmd = ['ssh', *self.opts, f'{self.user}@{host}', 'bash', '-s']
        self.hosts.add(host)
        for attempt in (1, 2):
            try:
                p = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return 124, '', f'timed out after {timeout}s'
            if p.returncode != 255 or attempt == 2:
                return p.returncode, p.stdout, p.stderr
            time.sleep(2)

    @staticmethod
    def transport_error(rc, err):
        """The ssh-level failure message, or None if the command itself ran."""
        if rc != 255:
            return None
        return (err.strip().splitlines() or ['ssh failed'])[-1].removeprefix('ssh: ')

    def close(self):
        """Tear down the shared connections and their sockets."""
        for host in self.hosts:
            try:
                subprocess.run(['ssh', *self.opts, '-O', 'exit', f'{self.user}@{host}'],
                               capture_output=True, text=True, timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass                    # the socket dir goes away regardless
        shutil.rmtree(self.sockdir, ignore_errors=True)

    def read_file(self, host, path, timeout=SSH_TIMEOUT):
        """Fetch one remote file's text, or None. Read through ssh rather
        than scp so nothing lands on local disk: config.json holds passwords."""
        rc, out, err = self.ssh(host, f'cat {shlex.quote(path)}', timeout)
        return out if rc == 0 else None


def hostname_short(name):
    return name.split('.')[0]


# ---- collectors -------------------------------------------------------------

def collect_swarm(remote, manager, rep):
    """Services, their replicas/images/constraints, the node each runs on,
    and the node-name -> host.role label map."""
    script = r"""
set -o pipefail
names=$(docker service ls --format '{{.Name}}')
echo "@@NODES"
for n in $(docker node ls -q); do
  docker node inspect "$n" --format '{{.Description.Hostname}}'$'\t''{{.Status.State}}'$'\t''{{.Spec.Availability}}'$'\t''{{if .ManagerStatus}}{{.ManagerStatus.Leader}}{{else}}worker{{end}}'$'\t''{{index .Spec.Labels "host.role"}}'
done
echo "@@SERVICES"
docker service ls --format '{{.Name}}'$'\t''{{.Replicas}}'$'\t''{{.Image}}'
echo "@@INSPECT"
# One inspect per service: the summary line, then one ENV line per variable.
# Only the memory keys leave the host: the same Env block can carry NEO4J_AUTH.
for s in $names; do
  docker service inspect "$s" --format '{{.Spec.Name}}'$'\t''{{join .Spec.TaskTemplate.Placement.Constraints ","}}'$'\t''{{range .Spec.TaskTemplate.Networks}}{{.Target}} {{end}}'$'\t''{{range .Endpoint.VirtualIPs}}{{.NetworkID}}={{.Addr}} {{end}}'$'\t''{{range .Spec.TaskTemplate.ContainerSpec.Mounts}}{{if eq .Target "/data"}}{{.Source}}{{end}}{{end}}'$'\t''{{if .Spec.TaskTemplate.Resources}}{{if .Spec.TaskTemplate.Resources.Limits}}{{.Spec.TaskTemplate.Resources.Limits.MemoryBytes}}{{end}}{{end}}'$'\n''{{range .Spec.TaskTemplate.ContainerSpec.Env}}ENV'$'\t''{{$.Spec.Name}}'$'\t''{{.}}'$'\n''{{end}}'
done | awk -F'\t' '$1 != "ENV" || $3 ~ /^NEO4J_(dbms|server)_memory_(heap_max__size|pagecache_size)=/'
echo "@@TASKS"
tasks=$(docker service ps $(docker service ls -q) --no-trunc --filter desired-state=running --format '{{.ID}}'$'\t''{{.Name}}'$'\t''{{.Node}}'$'\t''{{.CurrentState}}' 2>/dev/null)
echo "$tasks"
echo "@@TASKTIMES"
[ -n "$tasks" ] && docker inspect $(echo "$tasks" | cut -f1) --format '{{.ID}}'$'\t''{{.Status.State}}'$'\t''{{.Status.Timestamp}}' 2>/dev/null
echo "@@NETWORKS"
docker network ls --no-trunc --format '{{.ID}}'$'\t''{{.Name}}'
"""
    rc, out, err = remote.ssh(manager, script, timeout=180)
    if rc != 0:
        rep.fatal(f'swarm: could not query the manager {manager}: {err.strip() or f"exit {rc}"}')
        return None

    sections = {}
    cur = None
    for line in out.splitlines():
        if line.startswith('@@'):
            cur = line[2:]
            sections[cur] = []
        elif cur and line.strip():
            sections[cur].append(line.split('\t'))

    nodes = {}
    for host, state, avail, mgr, role in sections.get('NODES', []):
        nodes[host] = dict(short=hostname_short(host), state=state, availability=avail,
                           manager=(mgr != 'worker'), leader=(mgr == 'true'), role=role or '')
    services = {}
    for name, replicas, image in sections.get('SERVICES', []):
        services[name] = dict(name=name, replicas=replicas, image=image,
                              constraint='', node='', nodes=[], state='', networks=[], vips={},
                              data_dir='', mem_limit='', heap='', pagecache='', since='')
    for fields in sections.get('INSPECT', []):
        if fields[0] == 'ENV':
            _, svc, kv = fields
            key, _, value = kv.partition('=')
            if svc in services:
                services[svc]['pagecache' if 'pagecache' in key else 'heap'] = value
            continue
        name, constraint, nets, vips, data_dir, limit = fields
        if name in services:
            # Only a host.role placement is a node name. Any other constraint
            # (node.role==manager, node.hostname==...) is kept out of the
            # `node` column rather than mistaken for a host.
            m = re.search(r'host\.role\s*==\s*(\S+)', constraint)
            services[name]['constraint'] = m.group(1) if m else ''
            services[name]['networks'] = nets.split()
            # network id -> VIP address (without the /prefix). cypher-shell is
            # given the VIP rather than the service name because Java's URI
            # parser rejects an underscore in a hostname, and half the
            # services here have one (neo4j-manc-v1_2_3); the name then falls
            # back to localhost with a misleading "unable to connect".
            services[name]['vips'] = {}
            for pair in vips.split():
                nid, _, addr = pair.partition('=')
                services[name]['vips'][nid] = addr.split('/')[0]
            services[name]['data_dir'] = data_dir
            services[name]['mem_limit'] = human_bytes(limit)
    # Tasks report the node's hostname; the rest of the fleet speaks in
    # host.role labels. Translate where a label exists. A service
    # with several running tasks lists every node; `since` is the oldest.
    role_of = {n['short']: n['role'] for n in nodes.values()}
    started = {tid: ts for tid, state, ts in sections.get('TASKTIMES', []) if state == 'running'}
    for tid, task, node, state in sections.get('TASKS', []):
        name = re.sub(r'\.\d+$', '', task)
        if name in services:
            short = hostname_short(node)
            svc = services[name]
            svc['nodes'].append(role_of.get(short) or short)
            svc['node'] = ', '.join(sorted(set(svc['nodes'])))
            svc['state'] = state
            since = started.get(tid, '')[:10]                       # the date is enough
            if since and (not svc['since'] or since < svc['since']):
                svc['since'] = since
    networks = {nid: nname for nid, nname in sections.get('NETWORKS', [])}
    return dict(nodes=nodes, services=services, networks=networks)


def human_bytes(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ''
    return f'{n / 2**30:g}G' if n else ''


def memory_of(svc):
    """(heap, page cache, limit) as configured, each '' when unset."""
    return svc.get('heap') or '', svc.get('pagecache') or '', svc.get('mem_limit') or ''


def display_status(svc):
    """status_of, except that an Up store whose every Meta node carries
    hideDataSet=true reads Hidden: it runs and is served, but neuPrintExplorer
    does not list it. The judge keeps using status_of; hiding is intentional."""
    st = status_of(svc['replicas'])
    metas = svc.get('meta') or []
    if st == 'Up' and metas and all(m.get('hidden') for m in metas):
        return 'Hidden'
    return st


def served_by(svc, instances):
    """Which public endpoints list a dataset:tag this store holds."""
    held = {m['served'] for m in svc.get('meta') or []}
    hosts = []
    for inst in instances.values():
        ep = inst.get('endpoint') or {}
        if held & set(ep.get('datasets') or []):
            hosts.append(ep['host'])
    return ', '.join(sorted(hosts))


def status_of(replicas):
    if replicas is None:
        return 'Not deployed'
    if re.match(r'^0/0', replicas):
        return 'Down (0/0)'
    if re.match(r'^(\d+)/\1$', replicas) and not replicas.startswith('0/'):
        return 'Up'
    return f'Degraded ({replicas})'


def version_of(image):
    """'neo4j:4.4.16' -> '4.4.16'; the connectome-neuprint 3.5 image -> '3.5'."""
    tag = image.rsplit(':', 1)[-1] if ':' in image else ''
    tag = tag.split('@')[0]
    return tag


def instance_name(service):
    """em_services_neuprinthttp -> public; em_services_neuprinthttp-pre -> pre."""
    rest = service[len(HTTP_PREFIX):]
    return rest[1:] if rest.startswith('-') else 'public'


def config_dir(instance):
    return '/opt/conf/neuprinthttp' if instance == 'public' else f'/opt/conf/neuprinthttp-{instance}'


def bolt_service(url):
    """bolt://neo4j-vnc:7687 -> em_services_neo4j-vnc."""
    m = re.match(r'^bolt(?:\+s)?://([^:/]+)', url or '')
    if not m:
        return None
    host = m.group(1)
    return host if host.startswith(STACK) else f'{STACK}_{host}'


def collect_configs(remote, swarm, manager, rep):
    """For every neuprinthttp instance: its config.json from the node it runs
    on (or the manager, if it runs nowhere), parsed down to the stores."""
    instances = {}
    # Instances the swarm knows about. svc['node'] is already a role label
    # when the node has one; ssh needs the real hostname only if
    # the label does not resolve, which the fleet's DNS handles either way.
    for name, svc in swarm['services'].items():
        if name.startswith(HTTP_PREFIX):
            inst = instance_name(name)
            node = (svc['nodes'] or [svc['constraint'] or manager])[0]
            instances[inst] = dict(instance=inst, service=name, node=node, deployed=True)

    # Config directories on the manager with no service: Not deployed.
    rc, out, _ = remote.ssh(manager, 'ls -d /opt/conf/neuprinthttp* 2>/dev/null')
    for d in out.split():
        inst = 'public' if d.rstrip('/') == '/opt/conf/neuprinthttp' else d.rsplit('neuprinthttp-', 1)[-1]
        if inst not in instances:
            instances[inst] = dict(instance=inst, service=None, node=manager, deployed=False)
            rep.warn(f'config / {inst}: directory {d} exists on {manager} but no service is deployed')

    for inst in instances.values():
        path = f'{config_dir(inst["instance"])}/conf/config.json'
        text = remote.read_file(inst['node'], path)
        inst['path'] = f'{inst["node"]}:{path}'
        inst['main'] = None
        inst['alternatives'] = []
        inst['credentials'] = {}       # service -> (user, password); never printed
        inst['dvid'] = {}              # dataset:tag -> DVID server and node spec of its skeletons; tokens not kept
        if text is None:
            inst['error'] = 'unreadable'
            rep.warn(f'config / {inst["instance"]}: could not read {inst["path"]}')
            continue
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            inst['error'] = f'invalid JSON: {e.msg}'
            rep.warn(f'config / {inst["instance"]}: {inst["path"]} is not valid JSON ({e.msg})')
            continue

        def store(entry):
            ec = entry.get('engine-config', {}) or {}
            svc = bolt_service(ec.get('server'))
            if svc and ec.get('user'):
                inst['credentials'][svc] = (ec.get('user'), ec.get('password'))
            return dict(service=svc, server=ec.get('server'), database=ec.get('database'))

        inst['main'] = store(cfg)
        inst['alternatives'] = [store(a) for a in cfg.get('mainstore-alternatives', []) or []]
        # Where neuprinthttp fetches skeletons (and meshes) for each dataset:tag:
        # a DVID server and a node spec (a uuid, or <uuid-prefix>:<branch>).
        # That is the only place the fleet records which DVID repo a dataset
        # belongs to, so it is also where the uuid of a store can be checked.
        for kind in ('skeletons', 'roimeshes'):
            for entry in (cfg.get('datatypes') or {}).get(kind) or []:
                ec = (entry or {}).get('engine-config') or {}
                ds, server, node = ec.get('dataset'), ec.get('server'), ec.get('branch') or ec.get('uuid')
                if ds and server and node and ds not in inst['dvid']:
                    inst['dvid'][ds] = dict(server=str(server).rstrip('/'), node=str(node), kind=kind)
    return instances


def collect_stale_configs(remote, swarm, instances, rep):
    """Config directories on nodes that run no neuprinthttp instance at all.
    Harmless, but they are the copies a hand-edit lands in by mistake."""
    running_nodes = {i['node'] for i in instances.values() if i['deployed']}
    for host, n in swarm['nodes'].items():
        node = n['role'] or n['short']
        if node in running_nodes:
            continue
        rc, out, _ = remote.ssh(node, 'ls -d /opt/conf/neuprinthttp* 2>/dev/null')
        if rc == 0 and out.strip():
            names = ', '.join(Path(d).name for d in out.split())
            rep.warn(f'stale configs / {node}: runs no neuprinthttp, yet has {names}')


def http_json(url, headers=None, timeout=HTTP_TIMEOUT):
    """GET a JSON document -> (parsed, None) or (None, short error)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f'HTTP {e.code}'
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.gaierror):
            return None, f'name resolution failed ({reason.strerror})'
        return None, f'{type(reason).__name__}: {reason}'[:80]
    except Exception as e:                                  # noqa: BLE001
        return None, f'{type(e).__name__}: {e}'[:80]


def captured_time(last_edit):
    """Meta.lastDatabaseEdit -> when the build read DVID, to the second.

    The property reads '<mutation time> / <property time> (segment property
    update)'. The first half is the newest segmentation mutation the build
    saw and never changes afterwards; the second half is rewritten by
    neuprinthttp while the store runs, so it says nothing about the build."""
    first = (last_edit or '').split(' / ')[0].strip()
    m = re.match(r'(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)', first)
    return f'{m.group(1)} {m.group(2)}' if m else first


def collect_endpoints(instances, token, rep):
    """What each public neuprinthttp endpoint says about itself."""
    def get(host, path):
        return http_json(f'https://{host}.janelia.org/{path}',
                         headers={'Authorization': f'Bearer {token}'})

    def probe(inst):
        host = 'neuprint' if inst['instance'] == 'public' else f'neuprint-{inst["instance"]}'
        ep = dict(host=host, version=None, main=None, datasets=None, error=None)
        v, err = get(host, 'api/version')
        if err:
            ep['error'] = err
            return ep
        ep['version'] = (v or {}).get('Version')
        db, err = get(host, 'api/dbmeta/database')
        if not err:
            ep['main'] = bolt_service((db or {}).get('Location'))
        ds, err = get(host, 'api/dbmeta/datasets')
        if not err and isinstance(ds, dict):
            ep['datasets'] = sorted(ds.keys())
        else:
            ep['datasets'] = None
            ep['error'] = err or 'no datasets'
        return ep

    # The three requests to one host depend on each other; the hosts do not.
    # Sequentially, nine instances with a few of them down is minutes of
    # waiting on HTTP_TIMEOUT; in parallel it is the slowest single host.
    insts = list(instances.values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(insts))) as pool:
        for inst, ep in zip(insts, pool.map(probe, insts)):
            inst['endpoint'] = ep


def error_line(err, rc):
    """The line of stderr that says what went wrong. Docker 29 follows every
    error with "Run 'docker run --help' for more information", and
    cypher-shell can print a stack trace; neither is the message."""
    lines = [l.strip() for l in err.splitlines() if l.strip()]
    lines = [l for l in lines if not re.search(r"(Run|See) 'docker .*--help'", l)
             and not l.startswith('at ')]
    picked = next((l for l in lines if l.startswith('docker:')), lines[-1] if lines else f'exit {rc}')
    return re.sub(r'(?i)password\S*', 'password', picked)


PROBE_PARALLEL = 4          # cypher-shells at once inside the probe container
PROBE_MIN_AVAIL_GB = 32     # refuse to start a container below this MemAvailable
PROBE_MIN_FREE_GB = 16      # ... or below this MemFree: veth creation wants contiguous free pages now
PROBE_MAX_COMMIT_RATIO = 1.5  # ... or when Committed_AS exceeds CommitLimit by more than this


def host_safe_for_containers(remote, node, rep):
    """Pre-flight for the probe host: no kernel allocation failures in the
    last hour and enough memory of the right kind. On 2026-11-17 the manager
    was failing order-6 allocations for hours before a container start took
    the overlay network down with it; the tool had seen the symptom and
    probed anyway. That host had plenty *available* (reclaimable cache) but
    almost nothing *free*, and was committed to several times its limit, so
    MemAvailable alone would have passed it: all three are checked, and an
    unreadable dmesg only passes when every memory figure is comfortably
    good.
    Returns (ok, reason)."""
    script = r"""
now=$(date +%s)
recent=0; last=''
while IFS= read -r line; do
  ts=${line#[}; ts=${ts%%]*}
  t=$(date -d "$ts" +%s 2>/dev/null) || continue
  if [ $((now - t)) -lt 3600 ]; then recent=$((recent+1)); last=$ts; fi
done < <(dmesg -T 2>/dev/null | grep -i -E 'allocation failure|cannot allocate memory' | tail -50)
echo "dmesg_readable=$(dmesg -T >/dev/null 2>&1 && echo yes || echo no)"
echo "recent_failures=$recent"
echo "last_failure=$last"
awk '/^(MemAvailable|MemFree|Committed_AS|CommitLimit):/{gsub(":","",$1); print tolower($1)"_kb="$2}' /proc/meminfo
echo "uptime_days=$(awk '{printf "%d", $1/86400}' /proc/uptime)"
"""
    rc, out, err = remote.ssh(node, script)
    if rc != 0:
        return False, f'could not inspect {node}: {(err.strip().splitlines() or ["exit %d" % rc])[-1]}'
    facts = dict(l.split('=', 1) for l in out.splitlines() if '=' in l)
    gb = lambda key: int(facts.get(key, '0') or 0) / 2**20                  # noqa: E731
    avail_gb, free_gb = gb('memavailable_kb'), gb('memfree_kb')
    committed_gb, limit_gb = gb('committed_as_kb'), gb('commitlimit_kb')
    ratio = committed_gb / limit_gb if limit_gb else 0.0
    recent = int(facts.get('recent_failures', '0') or 0)
    memory = (f'{avail_gb:.0f} GB available, {free_gb:.0f} GB free, '
              f'committed {committed_gb:.0f} GB against a {limit_gb:.0f} GB limit')
    if recent:
        return False, (f'{node} logged {recent} kernel allocation failure(s) in the last hour '
                       f'(last at {facts.get("last_failure")}); starting a container there is unsafe')
    if avail_gb < PROBE_MIN_AVAIL_GB:
        return False, f'{node} has {memory}: below the {PROBE_MIN_AVAIL_GB} GB available floor for starting a container'
    if free_gb < PROBE_MIN_FREE_GB:
        return False, f'{node} has {memory}: below the {PROBE_MIN_FREE_GB} GB free floor for starting a container'
    if ratio > PROBE_MAX_COMMIT_RATIO:
        return False, f'{node} has {memory}: overcommitted {ratio:.1f}x, above the {PROBE_MAX_COMMIT_RATIO}x ceiling'
    if facts.get('dmesg_readable') != 'yes':
        # Without the kernel log a recent failure cannot be ruled out, so
        # only a host with twice the headroom on every figure gets the
        # benefit of the doubt.
        clearly_good = (avail_gb >= 2 * PROBE_MIN_AVAIL_GB and free_gb >= 2 * PROBE_MIN_FREE_GB
                        and ratio <= 1.0)
        if not clearly_good:
            return False, (f'{node}: dmesg is not readable by this account and the memory figures '
                           f'({memory}) are not good enough to skip that check')
        rep.warn(f'probe / {node} / dmesg: not readable by this account; proceeding only because memory is clearly good ({memory})')
    return True, f'{node}: {memory}, no allocation failures in the last hour, up {facts.get("uptime_days")} days'


PROBE_IMAGE = 'neo4j:4.4.16'
# One cypher-shell for every generation: 4.4's speaks Bolt to 3.5, 4.4 and
# 2026.08.1 servers alike (verified on wasp3:v0.0), so a run is one container.
# The CalVer image's own cypher-shell is not an option on the probe node:
# Docker 20.10's default seccomp profile predates the syscalls its Java needs
# ("We cannot execute /opt/java/openjdk/bin/java"); it runs only with
# --security-opt seccomp=unconfined, which the probe should not need.


def collect_meta(remote, swarm, instances, probe_node, rep, image=PROBE_IMAGE):
    """dataset/tag/uuid/... from every running neo4j, read over the overlay.

    One container is started on probe_node (not the swarm manager: see
    host_safe_for_containers) and probes every store from inside,
    PROBE_PARALLEL at a time. 28 stores therefore cost one container start,
    not 28. Returns (read, skipped): skipped > 0 means some stores were not
    attempted, because the container could not run or a store has no VIP."""
    read = skipped = 0
    # Which overlay network do the neo4j services share?
    net_ids = set()
    for svc in swarm['services'].values():
        if svc['name'].startswith(NEO4J_PREFIX):
            net_ids.update(svc['networks'])
    net_names = [swarm['networks'][n] for n in net_ids if n in swarm['networks']]
    if len(net_names) != 1:
        rep.warn(f'meta: expected one overlay network for the neo4j services, found {net_names or "none"}; skipping Meta reads')
        return 0, 0
    network = net_names[0]
    net_id = next(n for n in net_ids if swarm['networks'].get(n) == network)

    # Credentials and database name by service, from whichever config points at it.
    creds, dbname = {}, {}
    for inst in instances.values():
        creds.update(inst.get('credentials', {}))
        for store in [inst.get('main')] + list(inst.get('alternatives', [])):
            if store and store.get('service') and store.get('database'):
                dbname[store['service']] = store['database']

    # The stores to probe: every Up neo4j service with a VIP on the overlay.
    # The VIP is the only usable address (Java's URI parser rejects the
    # underscore in the service names), so a store without one is reported
    # as not read, not probed by name and then misreported as bolt-dead.
    stores = []
    for name, svc in sorted(swarm['services'].items()):
        if not name.startswith(NEO4J_PREFIX):
            continue
        svc['meta'] = None
        if status_of(svc['replicas']) != 'Up':
            continue
        version = version_of(svc['image'])
        short = name[len(STACK) + 1:]
        target = svc['vips'].get(net_id)
        if not target:
            svc['meta_error'] = f'not read: no VIP on {network}'
            rep.warn(f'meta / {short}: service is Up but has no VIP on {network}, so it was not probed')
            skipped += 1
            continue
        user, password = creds.get(name, (None, None))
        stores.append(dict(
            name=name, short=short, svc=svc, target=target,
            db=dbname.get(name) or ('data' if version[:2] == '20' else ''),
            user=user or '', password=password or ''))
    if not stores:
        return 0, skipped

    ok, reason = host_safe_for_containers(remote, probe_node, rep)
    if not ok:
        rep.warn(f'probe / {probe_node} / unsafe: {reason}; Meta not read for {len(stores)} running stores')
        for st in stores:
            st['svc']['meta_error'] = f'not read: {probe_node} unsafe for containers'
        return 0, skipped + len(stores)
    rep(f'probe:     {reason}')

    # The inner script runs in the probe container. Credentials are passed
    # to a shell function (not a process: nothing in /proc) and reach
    # cypher-shell through its environment. The array expansion is spelled
    # so that an empty dbflag is not "unbound" under set -u on bash < 4.4.
    inner = [
        'set -u', 'mkdir -p /tmp/o',
        f'CYPHER={shlex.quote(META_CYPHER)}',
        'run_store() {',
        '  local short=$1 target=$2 db=$3 user=$4 pass=$5',
        '  local dbflag=(); [ -n "$db" ] && dbflag=(-d "$db")',
        '  if [ -n "$pass" ]; then',
        f'    NEO4J_USERNAME="$user" NEO4J_PASSWORD="$pass" timeout {CYPHER_TIMEOUT} cypher-shell -a "bolt://$target:7687" ${{dbflag[@]+"${{dbflag[@]}}"}} --format plain "$CYPHER" >"/tmp/o/$short.out" 2>"/tmp/o/$short.err"',
        '  else',
        f'    timeout {CYPHER_TIMEOUT} cypher-shell -a "bolt://$target:7687" ${{dbflag[@]+"${{dbflag[@]}}"}} --format plain "$CYPHER" >"/tmp/o/$short.out" 2>"/tmp/o/$short.err"',
        '  fi',
        '  echo $? >"/tmp/o/$short.rc"',
        '}',
        'running=0',
    ]
    for st in stores:
        inner.append('run_store ' + ' '.join(shlex.quote(x) for x in
                                             [st['short'], st['target'], st['db'], st['user'], st['password']]) + ' &')
        inner.append(f'running=$((running+1)); if [ $running -ge {PROBE_PARALLEL} ]; then wait -n; running=$((running-1)); fi')
    inner.append('wait')
    for st in stores:
        inner.append(f'echo "@@STORE {st["short"]} rc=$(cat /tmp/o/{st["short"]}.rc 2>/dev/null || echo 99)"; '
                     f'cat /tmp/o/{st["short"]}.out; echo "@@ERR"; cat /tmp/o/{st["short"]}.err; echo "@@END"')
    inner_script = '\n'.join(inner) + '\n'
    # The container runs at most ceil(n / PROBE_PARALLEL) rounds of stores,
    # each bounded by CYPHER_TIMEOUT; the outer timeout allows for all of
    # them plus a container start, so a run with several hung stores still
    # reports the stores that did answer.
    rounds = -(-len(stores) // PROBE_PARALLEL)
    outer_timeout = rounds * CYPHER_TIMEOUT + 60
    # The outer script runs on the probe host. The inner script is a bash
    # string literal piped into the container: no argv, no temp file.
    outer = (f'INNER={shlex.quote(inner_script)}\n'
             f'printf %s "$INNER" | timeout {outer_timeout} docker run -i --rm '
             f'--network {shlex.quote(network)} {shlex.quote(image)} bash -s\n')
    # One retry: the whole pass rides on one container start, so a transient
    # failure there (an image pull hiccup, a dropped ssh session) should not
    # cost every Meta read. A timeout is not retried; it would just repeat.
    for attempt in (1, 2):
        rc, out, err = remote.ssh(probe_node, outer, timeout=outer_timeout + 60)
        ssh_failed = remote.transport_error(rc, err)
        parsed = parse_probe_output(out)
        failed = ssh_failed or (rc != 0 and not parsed)
        if not failed or rc == 124 or attempt == 2:
            break
        rep.warn(f'probe / {probe_node} / {image}: container did not run on the first try '
                 f'({(ssh_failed or error_line(err, rc))[:200]}); retrying once')
    if failed:
        if rc == 124:
            msg, what = f'timed out after {outer_timeout}s', 'did not finish'
        else:
            msg, what = ssh_failed or error_line(err, rc), 'did not run'
        rep.warn(f'probe / {probe_node} / {image}: container {what} ({msg[:200]}); '
                 f'Meta not read for {len(stores)} store(s): {", ".join(st["short"] for st in stores)}')
        for st in stores:
            st['svc']['meta_error'] = f'not read: probe container on {probe_node} {what}'
        return 0, skipped + len(stores)
    for st in stores:
        svc, short, target = st['svc'], st['short'], st['target']
        res = parsed.get(short)
        if res is None:
            svc['meta_error'] = 'not read: no result from the probe container'
            rep.warn(f'meta / {short}: the probe container returned nothing for it')
            skipped += 1
            continue
        src, sout, serr = res
        if src != 0:
            msg = f'timed out after {CYPHER_TIMEOUT}s' if src == 124 else error_line(serr, src)
            svc['meta_error'] = msg[:300]
            if src == 124:
                # bolt accepted the connection but the one-row Meta query never
                # finished: to neuprinthttp this store is as good as down.
                svc['bolt_down'] = True
                rep.diff(f'bolt / {short} / timeout: service is Up but a Meta query over bolt://{target}:7687 did not finish in {CYPHER_TIMEOUT}s')
            elif 'Unable to connect' in msg:
                # The task is running but nothing answers on bolt. neuprinthttp
                # cannot reach it either, so whatever this store holds is not
                # being served -- which is a fault, not a reporting gap.
                svc['bolt_down'] = True
                rep.diff(f'bolt / {short} / unreachable: service is Up but bolt://{target}:7687 does not answer, so nothing it holds is served')
            else:
                rep.warn(f'meta / {short}: could not read Meta ({svc["meta_error"]})')
            continue
        metas = []
        for row in (l for l in sout.splitlines() if l.strip() and l.strip() != 'row'):
            cells = row.strip().strip('"').split('|')
            if len(cells) == len(META_FIELDS):
                m = dict(zip(META_FIELDS, cells))
                ds, tag = m['dataset'], m['tag']
                metas.append(dict(dataset=ds, tag=tag, uuid=m['uuid'],
                                  hidden=m['hideDataSet'].lower() == 'true',
                                  pre=m['totalPreCount'], post=m['totalPostCount'],
                                  mutation=m['latestMutationId'],
                                  captured=captured_time(m['lastDatabaseEdit']),
                                  dvid=None,
                                  served=f'{ds}:{tag}' if tag else ds))
        svc['meta'] = metas
        read += 1
        if not metas:
            rep.warn(f'meta / {short}: no :Meta node found')
    return read, skipped


def dvid_host(server):
    return re.sub(r'^https?://', '', server).split('/')[0]


def is_dvid_spec(spec):
    """A DVID node spec: a hex uuid (or prefix), or <prefix>:<branch>. Not
    'v783b' or 'v888', which name a bucket version for a non-DVID source."""
    spec = spec or ''
    return bool(re.fullmatch(r'[0-9a-f]{4,32}', spec) or re.fullmatch(r'[0-9a-f]{4,32}:\S+', spec))


def short_error(err):
    e = err or ''
    if 'Connection refused' in e:
        return 'connection refused'
    if 'timed out' in e.lower() or e.startswith('TimeoutError'):
        return 'timed out'
    return e


def collect_dvid(swarm, instances, rep):
    """Is the uuid each running store was built from a locked DVID node?

    Looked up with GET /api/repo/<node>/info on the DVID server that the
    instance configs name as the skeleton source for that dataset:tag; the
    repo's DAG lists every node with its Locked flag, so one fetch answers
    for every store built from that repo. The configs' own node specs are
    resolved too (a uuid prefix, or <prefix>:<branch> meaning the head of
    that branch), so the judge can tell when skeletons are fetched from a
    different node than the store was built from. No DVID token is used:
    repo info is readable on the site network. Returns (uuids checked,
    servers asked)."""
    source = {}                                    # dataset:tag -> DVID server
    for inst in instances.values():
        for ds, d in (inst.get('dvid') or {}).items():
            source.setdefault(ds, d['server'])
    wanted = {}                                    # server -> set of node specs
    for svc in swarm['services'].values():
        for m in svc.get('meta') or []:
            m['dvid'] = None
            server = source.get(m['served']) or source.get(m['dataset'])
            if server and is_dvid_spec(m['uuid']):
                wanted.setdefault(server, set()).add(m['uuid'])
            elif server and m['uuid']:
                m['dvid'] = dict(error='not a DVID uuid')
    for inst in instances.values():
        for d in (inst.get('dvid') or {}).values():
            d['resolved'] = None
            if is_dvid_spec(d['node']):
                wanted.setdefault(d['server'], set()).add(d['node'])
    if not wanted:
        return 0, 0

    def resolve_in(repo, spec):
        """A node spec against one repo's DAG -> node dict or None."""
        nodes = (repo.get('DAG') or {}).get('Nodes') or {}
        if ':' in spec:
            prefix, branch = spec.split(':', 1)
            if not (repo.get('Root') or '').startswith(prefix) and not any(u.startswith(prefix) for u in nodes):
                return None
            want = '' if branch == 'master' else branch
            members = [n for n in nodes.values() if (n.get('Branch') or '') == want]
            leaves = [n for n in members if not n.get('Children')]
            heads = sorted(leaves or members, key=lambda n: (n.get('Locked', True), -(n.get('VersionID') or 0)))
            return heads[0] if heads else None
        hits = [n for u, n in nodes.items() if u.startswith(spec)]
        return hits[0] if len(hits) == 1 else None

    def describe(repo, node):
        # ancestors: this node and everything upstream of it in the DAG, so
        # two nodes of one repo can be placed in the same lineage or not.
        nodes = (repo.get('DAG') or {}).get('Nodes') or {}
        by_vid = {str(n.get('VersionID')): u for u, n in nodes.items()}
        ancestors, stack = set(), [node]
        while stack:
            n = stack.pop()
            u = n.get('UUID', '')
            if u in ancestors:
                continue
            ancestors.add(u)
            stack.extend(nodes[by_vid[str(v)]] for v in n.get('Parents') or [] if str(v) in by_vid and by_vid[str(v)] in nodes)
        return dict(uuid=node.get('UUID', ''), locked=node.get('Locked'),
                    branch=(node.get('Branch') or 'master'), head=repo.get('MutationID'),
                    root=repo.get('Root'), ancestors=ancestors,
                    note=(node.get('Note') or '').strip())

    def ask(server):
        """Every spec wanted on one server -> {spec: result}; one fetch per repo."""
        repos, out = [], {}
        for spec in sorted(wanted[server]):
            node, repo_hit = None, None
            for repo in repos:
                node = resolve_in(repo, spec)
                if node:
                    repo_hit = repo
                    break
            if node is None:
                repo, err = http_json(f'{server}/api/repo/{spec}/info', timeout=DVID_TIMEOUT)
                if err:
                    err = short_error(err)
                    out[spec] = dict(error=err)
                    if err in ('connection refused', 'timed out') or err.startswith('name resolution'):
                        for rest in wanted[server]:
                            out.setdefault(rest, dict(error=err))
                        break
                    continue
                repos.append(repo)
                node, repo_hit = resolve_in(repo, spec), repo
            out[spec] = describe(repo_hit, node) if node else dict(error='node not in the repo DAG')
        return out

    servers = sorted(wanted)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(servers)) as pool:
        resolved = dict(zip(servers, pool.map(ask, servers)))

    checked = 0
    for svc in swarm['services'].values():
        for m in svc.get('meta') or []:
            server = source.get(m['served']) or source.get(m['dataset'])
            if not (m['uuid'] and server) or m['dvid'] is not None:
                continue
            r = resolved.get(server, {}).get(m['uuid']) or dict(error='no answer')
            if not r.get('error') and r.get('locked') is False and r.get('head') and str(m.get('mutation') or '').isdigit():
                r['since'] = max(0, int(r['head']) - int(m['mutation']))
            m['dvid'] = r
            checked += not r.get('error')
    for inst in instances.values():
        for d in (inst.get('dvid') or {}).values():
            d['resolved'] = resolved.get(d['server'], {}).get(d['node'])
    for server in servers:
        errs = {r['error'] for r in resolved[server].values() if r.get('error')}
        for err in sorted(errs):
            n = sum(1 for r in resolved[server].values() if r.get('error') == err)
            rep.warn(f'dvid / {dvid_host(server)} / {err}: {n} node lookup(s) on this DVID server failed, so those uuids are not checked')
    return checked, len(servers)


def parse_probe_output(out):
    """@@STORE <short> rc=<n> / stdout / @@ERR / stderr / @@END blocks -> {short: (rc, out, err)}."""
    result, cur, mode = {}, None, None
    for line in out.splitlines():
        m = re.match(r'^@@STORE (\S+) rc=(\d+)$', line)
        if m:
            cur, mode = [m.group(1), int(m.group(2)), [], []], 'out'
            continue
        if cur is None:
            continue
        if line == '@@ERR':
            mode = 'err'
        elif line == '@@END':
            result[cur[0]] = (cur[1], '\n'.join(cur[2]), '\n'.join(cur[3]))
            cur = mode = None
        else:
            (cur[2] if mode == 'out' else cur[3]).append(line)
    return result


# ---- verdicts ---------------------------------------------------------------

def short_name(service):
    """em_services_neo4j-vnc -> neo4j-vnc."""
    return service[len(STACK) + 1:] if (service or '').startswith(STACK + '_') else (service or '?')


def judge(swarm, instances, rep):
    """Every finding is written `<kind> / <who> / <what>: <detail>`, so the
    part before the colon is unique and can be pasted into a baseline file."""
    services = swarm['services']
    neo4j = {n: s for n, s in services.items() if n.startswith(NEO4J_PREFIX)}

    # Which configs reference each neo4j service, and as what.
    referenced = {n: [] for n in neo4j}
    for inst in instances.values():
        who = inst['instance']
        for kind, store in [('M', inst.get('main'))] + [('A', a) for a in inst.get('alternatives', [])]:
            if not store or not store['service']:
                continue
            svc = store['service']
            if svc not in neo4j:
                rep.diff(f'config / {who} / {short_name(svc)}: {kind} points at a service the swarm does not have')
                continue
            referenced[svc].append((who, kind))
            st = status_of(neo4j[svc]['replicas'])
            if st != 'Up':
                rep.diff(f'config / {who} / {short_name(svc)}: {kind} points at a service that is {st}')
            # A CalVer store is built as database "data". The config may name
            # it, or the server may default to it (NEO4J_dbms_default__database
            # in the stack file); omitted on a server whose default is still
            # neo4j, the backend connects and finds nothing, silently. The
            # endpoint check below is what proves it either way.
            if version_of(neo4j[svc]['image'])[:2] == '20' and not store.get('database'):
                rep.warn(f'config / {who} / {short_name(svc)}: Neo4j {version_of(neo4j[svc]["image"])} store named without a "database" key; relies on the server default')

    unreadable = sorted(i['instance'] for i in instances.values() if i.get('error'))
    if unreadable:
        rep.warn(f'service / unreferenced / skipped: config.json unreadable for {", ".join(unreadable)}, '
                 'so a store no readable config names cannot be called unreferenced')
    for name, svc in neo4j.items():
        short = short_name(name)
        st = status_of(svc['replicas'])
        if st == 'Up' and not referenced[name] and not unreadable:
            rep.diff(f'service / {short} / unreferenced: Up but no neuprinthttp config points at it')
        if st == 'Up' and svc['constraint'] and svc['nodes'] and any(n != svc['constraint'] for n in svc['nodes']):
            rep.diff(f'placement / {short} / {svc["constraint"]}: constrained there but running on {svc["node"]}')

    # Endpoint vs config: the main store the server reports should be the
    # config's main store, and every served dataset should come from a store
    # the config names.
    served_by = {}                                              # dataset:tag -> [service]
    for name, svc in neo4j.items():
        for m in svc.get('meta') or []:
            served_by.setdefault(m['served'], []).append(short_name(name))
    for served, svcs in sorted(served_by.items()):
        if len(svcs) > 1:
            rep.diff(f'meta / {served} / duplicate: held by more than one running service: {", ".join(svcs)}')

    # Provenance. A tagged dataset is a release: it should rest on a locked
    # DVID node, or the tag names data that can still change under it. Two
    # stores sharing a uuid should have seen the same mutations, or the uuid
    # is a moving head and "same uuid" proves nothing. And the skeletons an
    # instance serves for a dataset:tag should come from the node the store
    # was built from, or body ids will not line up.
    by_uuid = {}
    for name, svc in neo4j.items():
        for m in svc.get('meta') or []:
            if m['uuid']:
                by_uuid.setdefault(m['uuid'], []).append((m, short_name(name)))
            dv = m.get('dvid') or {}
            if m['tag'] and dv.get('locked') is False:
                since = f', {dv["since"]} mutation(s) in the repo since' if dv.get('since') is not None else ''
                rep.diff(f'uuid / {m["served"]} / unlocked: built from DVID node {m["uuid"][:8]}, '
                         f'which is not locked (head of {dv["branch"]}{since}); a tag on a moving node')
    for uuid, holders in sorted(by_uuid.items()):
        muts = {m['mutation'] for m, _ in holders if m.get('mutation')}
        if len(holders) > 1 and len(muts) > 1:
            who = ', '.join(f'{m["served"]}@{svc} ({m["mutation"] or "?"})' for m, svc in holders)
            rep.warn(f'uuid / {uuid[:8]} / drift: same DVID node, different mutation ids: {who}; '
                     'it is an unlocked node read at different times')
    # Skeletons are routinely fetched from a node downstream of the store's
    # (regenerated later on the same lineage, body ids unchanged); that is
    # fine. A node upstream of the store, or on another lineage altogether,
    # is skeletons for a different segmentation.
    for inst in instances.values():
        for ds, d in sorted((inst.get('dvid') or {}).items()):
            r = d.get('resolved') or {}
            if not r or r.get('error'):
                continue
            for m, svc in [(m, s) for ms in by_uuid.values() for m, s in ms if m['served'] == ds]:
                sr = m.get('dvid') or {}
                if r['uuid'] == m['uuid'] or sr.get('error') or not sr.get('ancestors'):
                    continue
                if m['uuid'] in r['ancestors']:
                    continue                                    # downstream of the store: fine
                if r['uuid'] in sr['ancestors']:
                    rel = "upstream of the store's node, so they predate the store"
                else:
                    rel = "not in the store's lineage"
                rep.warn(f'dvid / {inst["instance"]} / {ds}: {d["kind"]} are fetched from DVID node '
                         f'{r["uuid"][:8]} ({d["node"]}), {rel}; the store on {svc} was built from {m["uuid"][:8]}')

    for inst in instances.values():
        ep = inst.get('endpoint') or {}
        if not inst['deployed'] or not ep:
            continue
        host = ep.get('host')
        if ep.get('error'):
            rep.warn(f'endpoint / {host} / unreachable: {ep["error"]}')
            continue
        main = (inst.get('main') or {}).get('service')
        if ep.get('main') and main and ep['main'] != main:
            rep.diff(f'endpoint / {host} / main: reports {short_name(ep["main"])} but config.json says {short_name(main)}')
        stores = [main] + [a['service'] for a in inst.get('alternatives', [])]
        explained = set()
        unknown_meta = False                # a store that is Up and answers, but Meta was not read
        for s in stores:
            if s in neo4j and status_of(neo4j[s]['replicas']) == 'Up':
                if neo4j[s].get('meta') is None and not neo4j[s].get('bolt_down'):
                    unknown_meta = True
                for m in neo4j[s].get('meta') or []:
                    explained.add(m['served'])
        if ep.get('datasets') is not None and not unknown_meta:
            # A store whose bolt does not answer holds nothing the endpoint
            # can serve, so it cannot explain a served dataset; and anything
            # it does hold is already reported under bolt/, not again here.
            for ds in ep['datasets']:
                if ds not in explained:
                    rep.diff(f'endpoint / {host} / {ds}: served, but no running store in its config holds it')
            for ds in sorted(explained - set(ep['datasets'])):
                rep.diff(f'endpoint / {host} / {ds}: held by a store in its config, but not served')


# ---- outputs ----------------------------------------------------------------

def instance_columns(instances):
    known = [i for i in INSTANCE_ORDER if i in instances]
    extra = sorted(i for i in instances if i not in INSTANCE_ORDER)
    return known + extra


def neo4j_rows(swarm, instances):
    """Every neo4j service that should appear in the table: the ones the
    swarm has, plus the ones some config names but the swarm does not have.
    The latter get a placeholder whose status reads "Not deployed", so a
    retired or never-created store still shows up as a row (with its M/A
    marks) instead of only as a finding."""
    rows = {n: s for n, s in swarm['services'].items() if n.startswith(NEO4J_PREFIX)}
    for inst in instances.values():
        stores = [inst.get('main')] + list(inst.get('alternatives', []))
        for store in stores:
            svc = (store or {}).get('service')
            if svc and svc.startswith(NEO4J_PREFIX) and svc not in rows:
                rows[svc] = dict(name=svc, replicas=None, image='', constraint='', node='',
                                 state='', networks=[], vips={}, meta=None)
    return [rows[n] for n in sorted(rows)]


def write_csv(path, swarm, instances, notes):
    cols = instance_columns(instances)
    header = ['neo4j service', 'Status', 'neo4j version', 'node', 'running on', 'since', 'serves', 'served by',
              'uuid', 'dvid node', 'mutation id', 'captured', 'pre / post', 'data dir',
              'heap', 'page cache', 'memory limit'] + cols + ['notes']
    rows = []
    for svc in neo4j_rows(swarm, instances):
        name = svc['name']
        short = name[len(STACK) + 1:]
        marks = {c: '' for c in cols}
        for inst in instances.values():
            c = inst['instance']
            if (inst.get('main') or {}).get('service') == name:
                marks[c] = 'M'
            elif any(a['service'] == name for a in inst.get('alternatives', [])):
                marks[c] = 'A'
        metas = svc.get('meta') or []
        join = lambda key: ', '.join(m[key] for m in metas)          # noqa: E731
        pre_post = ', '.join(f'{m["pre"]} / {m["post"]}' for m in metas if m['pre'] or m['post'])
        rows.append([short, display_status(svc), version_of(svc['image']), svc['constraint'],
                     svc['node'], svc.get('since', ''), join('served'), served_by(svc, instances),
                     join('uuid'), ', '.join(dvid_state(m)[0] for m in metas), join('mutation'), join('captured'),
                     pre_post, svc.get('data_dir', ''), *memory_of(svc)]
                    + [marks[c] for c in cols] + [notes.get(short, '')])
    # The http row from the original sheet: service status per instance.
    http_row = ['http server (neuprinthttp service)'] + [''] * (len(header) - len(cols) - 2)
    for c in cols:
        inst = instances[c]
        svc = swarm['services'].get(inst['service']) if inst['service'] else None
        http_row.append(status_of(svc['replicas']) if svc else 'Not deployed')
    http_row.append('')
    rows.append(http_row)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
        w.writerow([])
        w.writerow(['Key: M = main server in the config; A = entry in mainstore-alternatives.'])
        w.writerow(['Status is from docker service ls: Up = n/n replicas; Hidden = Up but Meta.hideDataSet is true, so neuPrintExplorer does not list it; Down (0/0) = scaled to zero; Not deployed = named by a config but not a swarm service.'])
        w.writerow(['neo4j version is the image tag from docker service ls (blank = not deployed). node is the host.role placement constraint; running on is the node the task is actually on; since is the date the running task started.'])
        w.writerow(['serves, uuid, mutation id, captured and pre / post come from each running store\'s :Meta node (dataset:tag, uuid, latestMutationId, the mutation half of lastDatabaseEdit, totalPreCount / totalPostCount), read over the swarm network.'])
        w.writerow(['dvid node is the Locked flag of the uuid in its DVID repo (GET /api/repo/<uuid>/info on the server the config names for its skeletons): locked, or UNLOCKED with the number of repo mutations since the build; blank when no config names a DVID server for the dataset:tag.'])
        w.writerow(['served by lists the public endpoints whose /api/dbmeta/datasets includes a dataset:tag this store holds.'])
        w.writerow(['data dir is the volume (or host path) mounted at /data. heap, page cache and memory limit are configured, not measured: the first two from the service environment (NEO4J_*_memory_heap_max__size, NEO4J_*_memory_pagecache_size), the last from the swarm resource limit.'])


def load_notes(path):
    """Notes column from a previous CSV, keyed by the service name."""
    notes = {}
    if not path:
        return notes
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            name = (row.get('neo4j service') or '').strip()
            note = (row.get('notes') or '').strip()
            if name and note:
                notes[name] = note
    return notes


def dvid_state(m):
    """One store's DVID lookup -> (plain text, kind) for the report and CSV."""
    dv = m.get('dvid')
    if dv is None:
        return ('', 'none')
    if dv.get('error'):
        return (f'not checked: {dv["error"]}', 'unknown')
    if dv.get('locked'):
        return ('locked', 'locked')
    since = f' (+{dv["since"]} since)' if dv.get('since') is not None else ''
    return (f'UNLOCKED{since}', 'unlocked')


def print_provenance(rep, swarm, instances):
    """Which DVID node each running store was built from, and its state there."""
    s = rep.s
    rows = []
    for svc in neo4j_rows(swarm, instances):
        for m in svc.get('meta') or []:
            rows.append((m['served'], svc['name'][len(STACK) + 1:], m))
    if not rows:
        return
    rep.heading('--- provenance: the DVID node each store was built from ---')
    w = max([22] + [len(r[0]) for r in rows])
    ws = max([24] + [len(r[1]) for r in rows])
    rep(s.dim(f'  {"dataset:tag":<{w}} {"store":<{ws}} {"uuid":<9} {"dvid node":<32} {"mutation id":<12} captured'))
    for served, short, m in sorted(rows):
        text, kind = dvid_state(m)
        text = (text or 'no DVID source in any config')[:32]
        cell = f'{text:<32}'
        cell = (s.green(cell) if kind == 'locked'
                else s.red(cell) if kind == 'unlocked' and m['tag']
                else s.yellow(cell) if kind == 'unlocked'
                else s.dim(cell))
        rep(f'  {served:<{w}} {short:<{ws}} {(m["uuid"] or "")[:8]:<9} {cell} {m.get("mutation") or "":<12} {m.get("captured") or ""}')


def print_report(rep, swarm, instances, manager, show_meta):
    s = rep.s
    rep.heading('--- swarm nodes ---')
    for host, n in sorted(swarm['nodes'].items(), key=lambda kv: kv[1]['role']):
        role = n['role'] or '(no label)'
        kind = 'leader' if n['leader'] else ('manager' if n['manager'] else 'worker')
        state = f'{n["state"]}/{n["availability"]}'
        state_s = s.green(state) if state == 'ready/active' else s.red(state)
        rep(f'  {role:<9} {n["short"]:<8} {kind:<8} {state_s}')

    cols = instance_columns(instances)
    rep.heading('--- neo4j services ---')
    rows = neo4j_rows(swarm, instances)
    w = max([24] + [len(r['name']) - len(STACK) - 1 for r in rows])
    rep(s.dim(f'  {"service":<{w}} {"status":<12} {"version":<10} {"node":<8} {"on":<8} {"since":<11} {"configs":<28} serves'))
    for svc in rows:
        name = svc['name']
        short = name[len(STACK) + 1:]
        st = display_status(svc)
        st_s = (s.green(f'{st:<12}') if st == 'Up' else s.yellow(f'{st:<12}') if st == 'Hidden'
                else s.dim(f'{st:<12}') if st.startswith('Down') else s.red(f'{st:<12}'))
        refs = []
        for c in cols:
            inst = instances[c]
            if (inst.get('main') or {}).get('service') == name:
                refs.append(f'{c}:M')
            elif any(a['service'] == name for a in inst.get('alternatives', [])):
                refs.append(f'{c}:A')
        refs_s = ' '.join(refs) if refs else (s.red('none') if st in ('Up', 'Hidden') else s.dim('none'))
        on = f'{svc["node"]:<8}' if svc['node'] else s.dim(f'{"-":<8}')
        if show_meta:
            if svc.get('meta') is None:
                served = s.dim('-' if st not in ('Up', 'Hidden') else svc.get('meta_error', '?'))
            else:
                served = ', '.join(m['served'] for m in svc['meta']) or s.yellow('no Meta')
        else:
            served = ''
        since = f'{svc["since"]:<11}' if svc.get('since') else s.dim(f'{"-":<11}')
        rep(f'  {short:<{w}} {st_s} {version_of(svc["image"]):<10} {svc["constraint"]:<8} {on} {since} {refs_s:<28} {served}')
    if show_meta:
        print_provenance(rep, swarm, instances)

    rep.heading('--- neuprinthttp instances ---')
    for c in cols:
        inst = instances[c]
        svc = swarm['services'].get(inst['service']) if inst['service'] else None
        st = status_of(svc['replicas']) if svc else 'Not deployed'
        st_s = s.green(st) if st == 'Up' else s.yellow(st)
        ep = inst.get('endpoint') or {}
        rep(f'  {c:<10} {st_s}  on {inst["node"]:<8} {s.dim(inst.get("path", ""))}')
        if inst.get('error'):
            rep(f'  {"":<10} config: {s.red(inst["error"])}')
        else:
            main = (inst.get('main') or {}).get('service') or '?'
            rep(f'  {"":<10} M {main[len(STACK) + 1:] if main.startswith(STACK) else main}'
                + (f'  {s.dim("db=" + inst["main"]["database"])}' if (inst.get('main') or {}).get('database') else ''))
            for a in inst.get('alternatives', []):
                nm = a['service'][len(STACK) + 1:] if (a['service'] or '').startswith(STACK) else (a['service'] or a['server'])
                rep(f'  {"":<10} A {nm}' + (f'  {s.dim("db=" + a["database"])}' if a.get('database') else ''))
        if ep.get('error'):
            rep(f'  {"":<10} endpoint {ep.get("host")}: {s.yellow(ep["error"])}')
        elif ep:
            rep(f'  {"":<10} endpoint {ep["host"]}: neuprinthttp {ep.get("version")}, '
                f'{len(ep.get("datasets") or [])} datasets: {s.dim(", ".join(ep.get("datasets") or []))}')


# ---- main -------------------------------------------------------------------

def main(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--manager', default=DEFAULT_MANAGER, metavar='HOST',
                   help=f'swarm manager to query (default {DEFAULT_MANAGER})')
    p.add_argument('--probe-node', default=DEFAULT_PROBE_NODE, metavar='HOST',
                   help=f'node that runs the Meta probe container (default {DEFAULT_PROBE_NODE}: an idle worker, never the manager)')
    p.add_argument('--probe-image', default=PROBE_IMAGE, metavar='IMAGE',
                   help=f'image whose cypher-shell reads :Meta from every store, one container for the whole run '
                        f'(default {PROBE_IMAGE}: its cypher-shell speaks Bolt to 3.5, 4.4 and 2026.x servers alike, '
                        f'and the CalVer image cannot start on the probe node, whose Docker 20.10 seccomp profile blocks its Java)')
    p.add_argument('--user', default=DEFAULT_USER, metavar='NAME', help=f'ssh account on the hosts (default {DEFAULT_USER})')
    p.add_argument('--no-meta', action='store_true', help='skip reading :Meta from each running neo4j')
    p.add_argument('--no-dvid', action='store_true',
                   help='skip asking the DVID servers whether each store\'s uuid is a locked node')
    p.add_argument('--no-endpoints', action='store_true', help='skip the https://neuprint-*.janelia.org probes')
    p.add_argument('--notes-from', metavar='CSV', help='copy the notes column from a previous CSV by service name')
    p.add_argument('--no-report', action='store_true', help='do not write results/ files')
    add_baseline_argument(p)
    add_color_argument(p)
    args = p.parse_args(argv[1:])

    rep = Reporter(Style(want_color(args.color)), baseline=load_baseline(args.baseline))
    token = os.environ.get('NEUPRINT_JWT') or os.environ.get('NEUPRINT_TOKEN')
    if not token and not args.no_endpoints:
        print('ERROR: set NEUPRINT_JWT (or NEUPRINT_TOKEN), or pass --no-endpoints.', file=sys.stderr)
        return 2
    remote = Remote(args.user, rep)
    try:
        return run(args, remote, token, rep)
    finally:
        remote.close()


def run(args, remote, token, rep):
    rep(f'manager:   {args.user}@{args.manager}')
    swarm = collect_swarm(remote, args.manager, rep)
    if swarm is None:
        rep.print_fatals()
        return 2
    n_neo4j = sum(1 for n in swarm['services'] if n.startswith(NEO4J_PREFIX))
    n_http = sum(1 for n in swarm['services'] if n.startswith(HTTP_PREFIX))
    rep(f'swarm:     {len(swarm["nodes"])} nodes, {len(swarm["services"])} services '
        f'({n_neo4j} neo4j, {n_http} neuprinthttp)')

    instances = collect_configs(remote, swarm, args.manager, rep)
    rep(f'configs:   {len(instances)} neuprinthttp instances read')
    collect_stale_configs(remote, swarm, instances, rep)
    if not args.no_endpoints:
        collect_endpoints(instances, token, rep)
    if not args.no_meta:
        up = [n for n, s in swarm['services'].items() if n.startswith(NEO4J_PREFIX) and status_of(s['replicas']) == 'Up']
        # Terminal only: the report file gets the probe and result lines below.
        print(f'meta:      reading :Meta from {len(up)} running neo4j services via {args.probe_node} ...', flush=True)
        read, skipped = collect_meta(remote, swarm, instances, args.probe_node, rep, image=args.probe_image)
        line = f'meta:      read :Meta from {read} of {len(up)} running neo4j services'
        if skipped:
            line += f' ({skipped} not attempted, see findings)'
        rep(line)
        if not args.no_dvid:
            checked, servers = collect_dvid(swarm, instances, rep)
            rep(f'dvid:      {checked} store uuid(s) looked up on {servers} DVID server(s)')

    print_report(rep, swarm, instances, args.manager, show_meta=not args.no_meta)
    judge(swarm, instances, rep)
    d, w = rep.print_findings()
    rep('')
    if d:
        rep(f'RESULT: {rep.s.red("INCONSISTENT")} -- {d} difference(s) between what the swarm runs, '
            'what the configs say and what is served. See findings.')
    else:
        rep(f'RESULT: {rep.s.green("CONSISTENT")} -- every running store is referenced, every reference '
            'resolves, and the endpoints serve what their stores hold.')
        if w:
            rep(rep.s.dim(f'        ({w} warning(s) above.)'))

    if not args.no_report:
        from datetime import datetime
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        out_dir = Path(__file__).resolve().parent.parent / 'results'
        txt = out_dir / f'fleet-{stamp}.txt'
        csvp = out_dir / f'fleet-{stamp}.csv'
        write_csv(csvp, swarm, instances, load_notes(args.notes_from))
        rep(rep.s.dim(f'report: {txt}'))
        rep(rep.s.dim(f'csv:    {csvp}'))
        rep.write(txt)
    return 1 if d else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

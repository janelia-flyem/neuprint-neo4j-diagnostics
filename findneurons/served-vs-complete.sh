#!/usr/bin/env bash
#
# For every dataset on a neuPrint server: work out which query the client would
# use, run that query, and compare it against buildSlowQuery as ground truth.
# The LOST column is what a user actually fails to see.
#
#   ./served-vs-complete.sh <server> [term] [queries-dir]
#
# The queries directory comes from extract-queries.sh, and which git ref you
# extracted decides whose behaviour is being measured:
#
#   extract-queries.sh <repo> d027001^          -> the pre-#383 client
#   extract-queries.sh <repo> origin/master     -> whatever is on master now
#
# The decision logic is inferred from the capability queries that were
# extracted, so it follows the client rather than being hardcoded here:
#   * index.cypher asking for `properties`  -> coverage is required
#   * index.cypher calling db.indexes()     -> name existence only
#
# Auth: set NEUPRINT_JWT (or NEUPRINT_TOKEN) for non-public servers. The token
# is used only in a request header -- never printed, written to a file, or put
# on a command line, so this output is safe to share. AUDIT AUTHENTICATED:
# /api/dbmeta/datasets returns fewer datasets to an anonymous client, and the
# worst case we found was among the hidden ones.
set -uo pipefail

S=${1:?usage: served-vs-complete.sh <server> [term] [queries-dir]}
TERM=${2:-a}
Q=${3:-./queries}
TOKEN=${NEUPRINT_JWT:-${NEUPRINT_TOKEN:-}}
AUTH=(); [ -n "${TOKEN}" ] && AUTH=(-H "Authorization: Bearer ${TOKEN}")
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT

for f in fast slow version index; do
    [ -r "$Q/$f.cypher" ] || { echo "ERROR: $Q/$f.cypher missing -- run extract-queries.sh first" 1>&2; exit 2; }
done

# The eleven properties both search queries rank on.
PROPS='type instance hemibrainType flywireType systematicType itoleeHl trumanHl synonyms class entryNerve exitNerve'

# Which style of capability check did this client use? Test for db.indexes()
# first, and match the coverage style on "state, properties" rather than on
# "properties" alone -- the index NAME contains that word, so a looser test
# misclassifies the old client and then fails unpacking its single column.
if grep -q 'db.indexes()' "$Q/index.cypher"; then
    STYLE=name-only
elif grep -q 'state, properties' "$Q/index.cypher"; then
    STYLE=coverage
else
    STYLE=state-only
fi

post() {  # $1=dataset $2=cypher-file
    python3 - "$1" "$2" > "$W/p.json" <<'PY'
import json, sys
print(json.dumps({"dataset": sys.argv[1], "cypher": open(sys.argv[2]).read()}))
PY
    curl -sS -m 300 -X POST "$S/api/custom/custom" -H 'Content-Type: application/json' \
         ${AUTH[@]+"${AUTH[@]}"} -d @"$W/p.json"
}

DS_LIST=$(curl -sS -m 60 "$S/api/dbmeta/datasets" ${AUTH[@]+"${AUTH[@]}"} | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
if not isinstance(d, dict) or d.get('error') or d.get('message') or not d: sys.exit(1)
print('\n'.join(sorted(d)))")
if [ -z "${DS_LIST}" ]; then
    echo "ERROR: could not list datasets -- not authorised, or server unreachable." 1>&2
    exit 1
fi

echo "server: $S    term: '$TERM'    capability check: $STYLE"
echo "auth: $([ -n "${TOKEN}" ] && echo 'bearer token supplied' || echo 'NONE -- dataset list may be incomplete')"
printf "%-24s %-10s %10s %10s %8s %8s\n" DATASET "QUERY USED" SERVED COMPLETE LOST "%"
printf '%.0s-' {1..74}; echo

while IFS= read -r DS; do
    [ -z "$DS" ] && continue
    post "$DS" "$Q/version.cypher" > "$W/v.json"
    post "$DS" "$Q/index.cypher"   > "$W/i.json"
    DEC=$(python3 - "$W/v.json" "$W/i.json" "$STYLE" "$PROPS" <<'PY'
import json, sys
def load(p):
    try: return json.load(open(p))
    except Exception: return {}
v, i, style, props = load(sys.argv[1]), load(sys.argv[2]), sys.argv[3], sys.argv[4].split()
rows = v.get('data') or []
ver = rows[0][0] if rows and not v.get('error') else None
if ver is None:
    print('slow'); raise SystemExit            # probe failed -> catch -> slow
try: num = float('.'.join(str(ver).split('.')[:2]))
except Exception: num = 0
if num < 4.4:
    print('slow'); raise SystemExit            # below the CALL {} floor
row = (i.get('data') or [None])[0]
if i.get('error') or not row:
    print('slow'); raise SystemExit            # no such index
try:
    if style == 'name-only':
        print('fast' if row[0] is True else 'slow')
    elif style == 'state-only':
        print('fast' if row[0] == 'ONLINE' else 'slow')
    else:
        state, properties = row[0], row[1] or []
        print('fast' if state == 'ONLINE' and all(p in properties for p in props) else 'slow')
except Exception:
    # An unexpected row shape is not evidence the fast path is usable.
    print('slow')
PY
)
    sed -e "s/TERM/$TERM/" -e "s/BODY/0/" "$Q/$DEC.cypher" > "$W/served.cypher"
    sed -e "s/TERM/$TERM/" -e "s/BODY/0/" "$Q/slow.cypher" > "$W/truth.cypher"
    post "$DS" "$W/served.cypher" > "$W/s.json"
    post "$DS" "$W/truth.cypher"  > "$W/t.json"
    python3 - "$DS" "$DEC" "$W/s.json" "$W/t.json" <<'PY'
import json, sys
ds, dec = sys.argv[1], sys.argv[2]
def rows(p):
    try: d = json.load(open(p))
    except Exception: return None
    if d.get('error'): return None
    return {r[0] for r in d.get('data') or []}
sv, tr = rows(sys.argv[3]), rows(sys.argv[4])
if sv is None or tr is None:
    print(f"{ds:<24} {dec:<10} {'query error':>10}"); raise SystemExit
lost = len(tr - sv)
pct = 100.0 * lost / len(tr) if tr else 0.0
print(f"{ds:<24} {dec:<10} {len(sv):>10} {len(tr):>10} {lost:>8} {pct:>7.1f}%")
PY
done <<< "${DS_LIST}"

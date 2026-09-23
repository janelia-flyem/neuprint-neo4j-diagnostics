#!/bin/bash
#
# Report, for every dataset on a neuPrint server, whether the FindNeurons
# fulltext index covers all eleven properties the search queries rank on.
# An index that exists but covers fewer makes the fast query silently return
# fewer rows than the slow one.
#
# Usage:
#   ./prod-coverage.sh https://neuprint-yakuba.janelia.org
#
# Auth: set NEUPRINT_JWT (or NEUPRINT_TOKEN) in the environment for the
# non-public servers. The token is read from the environment and used only in
# a request header -- it is never printed, written to a file, or placed on a
# command line, so the output of this script is safe to share.
set -uo pipefail
S=${1:-https://neuprint.janelia.org}
TOKEN=${NEUPRINT_JWT:-${NEUPRINT_TOKEN:-}}
AUTH=()
[ -n "${TOKEN}" ] && AUTH=(-H "Authorization: Bearer ${TOKEN}")
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
PROPS='type instance hemibrainType flywireType systematicType itoleeHl trumanHl synonyms class entryNerve exitNerve'

echo "server: $S    auth: $([ -n "${TOKEN}" ] && echo 'bearer token supplied' || echo none)"

post() {
  python3 - "$1" "$2" > "$W/p.json" <<'PY'
import json,sys
print(json.dumps({"dataset": sys.argv[1], "cypher": open(sys.argv[2]).read()}))
PY
    HTTP=$(curl -sS -m 300 -o "$W/resp.json" -w '%{http_code}' -X POST "$S/api/custom/custom" \
         -H 'Content-Type: application/json' \
         ${AUTH[@]+"${AUTH[@]}"} -d @"$W/p.json")
    if [ "$HTTP" != "200" ]; then
        # A 401 arrives as {"message":"authentication required"} with no
        # "error" key, so without this it reads as an empty result set and
        # gets reported as "nothing lost" -- a false all-clear.
        python3 -c "
import json,sys
print(json.dumps({'error': 'HTTP ' + sys.argv[2] + ': ' + open(sys.argv[1]).read()[:160]}))" \
            "$W/resp.json" "$HTTP"
    else
        cat "$W/resp.json"
    fi
}

DS_LIST=$(curl -sS -m 60 "$S/api/dbmeta/datasets" ${AUTH[@]+"${AUTH[@]}"} \
  | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
if not isinstance(d,dict) or d.get('error') or d.get('message'): sys.exit(1)
print('\n'.join(sorted(d)))")
if [ -z "${DS_LIST}" ]; then
  echo "ERROR: could not list datasets -- not authorised, or the server is unreachable." 1>&2
  exit 1
fi

echo "SHOW INDEXES YIELD name, state, properties WHERE name = 'find_neurons_fulltext_properties_index' RETURN state, properties" > "$W/idx.cypher"
printf "%-24s %-9s %-8s %s\n" DATASET STATE COVERED "UNCOVERED PROPERTIES"
printf '%.0s-' {1..100}; echo
while IFS= read -r DS; do
  [ -z "$DS" ] && continue
  post "$DS" "$W/idx.cypher" > "$W/i.json"
  python3 - "$DS" "$W/i.json" "$PROPS" <<'PY'
import json,sys
ds,f,props = sys.argv[1], sys.argv[2], sys.argv[3].split()
try: d=json.load(open(f))
except Exception: d={}
rows = d.get('data') or []
if d.get('error') or not rows:
    why = 'no such index' if not d.get('error') else 'query failed (3.5 server?)'
    print(f"{ds:<24} {'n/a':<9} {'-':<8} ({why})"); raise SystemExit
state, properties = rows[0][0], rows[0][1] or []
missing = [p for p in props if p not in properties]
print(f"{ds:<24} {state:<9} {str(len(props)-len(missing))+'/11':<8} "
      f"{', '.join(missing) if missing else '(complete)'}")
PY
done <<< "${DS_LIST}"

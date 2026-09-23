#!/bin/bash
#
# Measure how many search results the fast query loses against the slow query,
# for one dataset, across several terms. Shows how the loss depends on the
# term: a specific one may lose nothing while a broad one loses most of the
# result set.
#
# Queries come from extract-queries.sh; which git ref you extracted decides
# whose fast query is being measured.
#
#   ./term-loss.sh <server> <dataset> [term1,term2,...] [queries-dir]
#
# Reads NEUPRINT_JWT / NEUPRINT_TOKEN from the environment; the token is only
# ever used in a request header, never printed or written down. Output is row
# counts only, so it is safe to share.
set -uo pipefail
S=${1:?usage: term-loss.sh <server> <dataset> [terms] [queries-dir]}
DS=${2:?dataset required}
TERMS=${3:-lc,a,dn,ps}
Q=${4:-./queries}
TOKEN=${NEUPRINT_JWT:-${NEUPRINT_TOKEN:-}}
AUTH=(); [ -n "${TOKEN}" ] && AUTH=(-H "Authorization: Bearer ${TOKEN}")
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
post() {
  python3 - "$DS" "$1" > "$W/p.json" <<'PY'
import json,sys
print(json.dumps({"dataset": sys.argv[1], "cypher": open(sys.argv[2]).read()}))
PY
  HTTP=$(curl -sS -m 300 -o "$W/resp.json" -w '%{http_code}' -X POST "$S/api/custom/custom" \
       -H 'Content-Type: application/json' ${AUTH[@]+"${AUTH[@]}"} -d @"$W/p.json")
  if [ "$HTTP" != "200" ]; then
      # A 401 arrives as {"message":"authentication required"} with no
      # "error" key, so without this it reads as an empty result set and
      # is reported as "nothing lost" -- a false all-clear.
      python3 -c "
import json,sys
print(json.dumps({'error': 'HTTP ' + sys.argv[2] + ': ' + open(sys.argv[1]).read()[:160]}))" \
          "$W/resp.json" "$HTTP"
  else
      cat "$W/resp.json"
  fi
}
for f in fast slow; do
  [ -r "$Q/$f.cypher" ] || { echo "ERROR: $Q/$f.cypher missing -- run extract-queries.sh first" 1>&2; exit 2; }
done
echo "$DS on $S   (fast query vs slow query, from $Q)"
printf "%-10s %10s %10s %10s %s\n" TERM FAST SLOW LOST "% MISSING"
IFS=',' read -ra TL <<< "$TERMS"
for t in "${TL[@]}"; do
  for f in fast slow; do
    sed -e "s/TERM/$t/g" -e "s/BODY/0/g" "$Q/$f.cypher" > "$W/$f.cypher"
    post "$W/$f.cypher" > "$W/$f.json"
  done
  python3 - "$t" "$W/fast.json" "$W/slow.json" <<'PY'
import json,sys
t=sys.argv[1]
def rows(p):
    d=json.load(open(p))
    if d.get('error'): return None
    return {r[0] for r in d.get('data') or []}
f,s = rows(sys.argv[2]), rows(sys.argv[3])
if f is None or s is None:
    print(f"{t:<10} {'query error':>10}"); raise SystemExit
lost=len(s-f); pct=100.0*lost/len(s) if s else 0.0
print(f"{t:<10} {len(f):>10} {len(s):>10} {lost:>10} {pct:>8.1f}%")
PY
done

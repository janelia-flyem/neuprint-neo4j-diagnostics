#!/bin/bash
#
# Find a concrete, browser-observable case of the incomplete-index bug:
# a neuron whose ONLY match for a search term is in a property the fulltext
# index does not cover. Typing that term in the web UI will not offer it.
#
#   ./find-demo-case.sh <server> <dataset>
#
# Reads NEUPRINT_JWT / NEUPRINT_TOKEN; never prints or stores the token.
set -uo pipefail
S=$1; DS=$2
TOKEN=${NEUPRINT_JWT:-${NEUPRINT_TOKEN:-}}
AUTH=(); [ -n "${TOKEN}" ] && AUTH=(-H "Authorization: Bearer ${TOKEN}")
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
run() {
  printf '%s' "$1" > "$W/c.cypher"
  python3 - "$DS" "$W/c.cypher" > "$W/p.json" <<'PY'
import json,sys
print(json.dumps({"dataset": sys.argv[1], "cypher": open(sys.argv[2]).read()}))
PY
  curl -sS -m 180 -X POST "$S/api/custom/custom" -H 'Content-Type: application/json' \
       ${AUTH[@]+"${AUTH[@]}"} -d @"$W/p.json"
}

echo "=== most common 'class' values (class is indexed nowhere, but searched) ==="
run "MATCH (n:Neuron) WHERE n.class IS NOT NULL
RETURN n.class AS class, count(*) AS neurons
ORDER BY neurons DESC LIMIT 8" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('error'): print('  error:', d['error'][:120]); raise SystemExit
for c,n in d.get('data') or []: print(f'  {n:>8}  {c}')"

echo
echo "=== a neuron findable by class but NOT by type/instance/synonyms ==="
echo "    (type its class value in the search box: it should be offered, and will not be)"
run "MATCH (n:Neuron)
WHERE n.class IS NOT NULL
  AND NOT toLower(coalesce(n.type,''))     CONTAINS toLower(n.class)
  AND NOT toLower(coalesce(n.instance,'')) CONTAINS toLower(n.class)
  AND NOT toLower(coalesce(n.synonyms,'')) CONTAINS toLower(n.class)
RETURN toString(n.bodyId) AS bodyId, n.class AS class,
       coalesce(n.type,'(none)') AS type, coalesce(n.instance,'(none)') AS instance
LIMIT 5" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('error'): print('  error:', d['error'][:120]); raise SystemExit
rows=d.get('data') or []
if not rows: print('  none found -- class values all also appear in type/instance/synonyms here'); raise SystemExit
for b,c,t,i in rows:
    print(f'  bodyId {b}')
    print(f'    class    = {c!r}   <-- type this in the search box')
    print(f'    type     = {t!r}')
    print(f'    instance = {i!r}')
    print()"

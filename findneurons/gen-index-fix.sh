#!/bin/bash
#
# Print the Cypher needed to rebuild a dataset's FindNeurons fulltext index
# with all eleven searched properties. Reads the live index to get its label,
# so the statements are correct for the dataset rather than guessed.
#
#   ./gen-index-fix.sh <server> <dataset>
#
# PRINTS ONLY -- it executes nothing. Review the output before running it.
# Reads NEUPRINT_JWT / NEUPRINT_TOKEN; never prints or stores the token.
set -uo pipefail
S=$1; DS=$2
TOKEN=${NEUPRINT_JWT:-${NEUPRINT_TOKEN:-}}
AUTH=(); [ -n "${TOKEN}" ] && AUTH=(-H "Authorization: Bearer ${TOKEN}")
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
echo "SHOW INDEXES YIELD name, labelsOrTypes, properties, state WHERE name = 'find_neurons_fulltext_properties_index' RETURN labelsOrTypes, properties, state" > "$W/q.cypher"
python3 - "$DS" "$W/q.cypher" > "$W/p.json" <<'PY'
import json,sys
print(json.dumps({"dataset": sys.argv[1], "cypher": open(sys.argv[2]).read()}))
PY
curl -sS -m 120 -X POST "$S/api/custom/custom" -H 'Content-Type: application/json' \
     ${AUTH[@]+"${AUTH[@]}"} -d @"$W/p.json" > "$W/r.json"
python3 - "$DS" "$W/r.json" <<'PY'
import json,sys
ds,f = sys.argv[1], sys.argv[2]
d=json.load(open(f))
rows=d.get('data') or []
if d.get('error') or not rows:
    print("Could not read the index:", (d.get('error') or 'no such index')[:120]); raise SystemExit(1)
labels, props, state = rows[0][0], rows[0][1] or [], rows[0][2]
want = ['type','instance','hemibrainType','flywireType','systematicType',
        'itoleeHl','trumanHl','synonyms','class','entryNerve','exitNerve']
missing=[p for p in want if p not in props]
label = labels[0] if labels else f"{ds.split(':')[0]}_Neuron"
print(f"-- dataset {ds}: index is {state} on :{label}")
print(f"-- currently covers {len(want)-len(missing)}/11; missing: {', '.join(missing) or 'none'}")
if not missing:
    print("-- Nothing to do: the index already covers all eleven properties.")
    raise SystemExit
print("--")
print("-- Recreate it with all eleven. The new index builds in the background;")
print("-- while it is building, state is POPULATING. A client that checks state")
print("-- will use the slow query until it is ONLINE again, which is correct.")
print("-- Run these one at a time and confirm the second returns before use:")
print()
print("DROP INDEX find_neurons_fulltext_properties_index;")
print()
print(f"CREATE FULLTEXT INDEX find_neurons_fulltext_properties_index FOR (n:`{label}`)")
print("ON EACH [")
print(",\n".join(f"    n.`{p}`" for p in want))
print("];")
print()
print("-- then verify:")
print("SHOW INDEXES YIELD name, state, properties")
print("  WHERE name = 'find_neurons_fulltext_properties_index'")
print("  RETURN state, size(properties) AS property_count;")
PY

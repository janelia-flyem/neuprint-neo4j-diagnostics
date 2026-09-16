#!/bin/bash
#
# Demonstrate what an index on a label no node carries actually costs.
#
# This reproduces the `fish2_:Soma` defect: element labels are written in
# configs with a leading colon (':Soma'), which is Cypher punctuation rather
# than part of the label. flyem-snapshot's element exporter stripped it, so
# nodes carried `fish2_Soma`, while the index builder did not, so 217 indexes
# were created on `fish2_:Soma` -- a label nothing carried.
#
# The point of the demo is that Neo4j cannot tell you this has happened:
# SHOW INDEXES reports ONLINE at populationPercent 100.0, because an index
# over a label no node carries is trivially fully populated.
#
# Needs podman (or docker -- change the CLI below). Takes about two minutes.
set -euo pipefail

CLI=${CLI:-podman}
IMAGE=${IMAGE:-docker.io/library/neo4j:2026.08.1}
NAME=${NAME:-inert-index-demo}
NODES=${NODES:-190774}          # fish2's non-synaptic element count

run() { ${CLI} exec "${NAME}" cypher-shell "$@"; }

echo "== starting ${IMAGE}"
${CLI} rm -f "${NAME}" >/dev/null 2>&1 || true
${CLI} run -d --name "${NAME}" -e NEO4J_AUTH=none "${IMAGE}" >/dev/null
for _ in $(seq 60); do run "RETURN 1;" >/dev/null 2>&1 && break; sleep 2; done

echo "== creating ${NODES} :fish2_Soma nodes with a boolean ROI property"
run "UNWIND range(1, ${NODES}) AS i
     CREATE (n:\`fish2_Soma\` {bodyId: i, \`Forebrain\`: (i % 1000 = 0)});" >/dev/null

echo "== creating the BROKEN index, as the unfixed indexes.py rendered it"
run "CREATE INDEX FOR (n:\`fish2_:Soma\`) ON (n.\`Forebrain\`);" >/dev/null
run "CALL db.awaitIndexes(300);" >/dev/null

echo
echo "-- Neo4j's own view of that index:"
run --format plain \
  "SHOW INDEXES YIELD labelsOrTypes, properties, state, populationPercent
   WHERE 'Forebrain' IN properties
   RETURN labelsOrTypes[0] AS label, state, populationPercent;"
echo "   ^ ONLINE at 100% -- an index over a label nothing carries is"
echo "     trivially fully populated, so the metrics look healthy."

echo
echo "-- what a client query actually does (only the broken index exists):"
run --format verbose "PROFILE MATCH (n:\`fish2_Soma\`) WHERE n.\`Forebrain\` = true RETURN count(n);" \
  | grep -oE 'NodeByLabelScan|NodeIndexSeek|Filter' | sort -u | sed 's/^/     /'
run "PROFILE MATCH (n:\`fish2_Soma\`) WHERE n.\`Forebrain\` = true RETURN count(n);" \
  | grep -E '^(DbHits|Time|Rows)' | sed 's/^/     /'

echo
echo "== now adding the CORRECT index and repeating the identical query"
run "CREATE INDEX FOR (n:\`fish2_Soma\`) ON (n.\`Forebrain\`);" >/dev/null
run "CALL db.awaitIndexes(300);" >/dev/null
run --format verbose "PROFILE MATCH (n:\`fish2_Soma\`) WHERE n.\`Forebrain\` = true RETURN count(n);" \
  | grep -oE 'NodeByLabelScan|NodeIndexSeek|Filter' | sort -u | sed 's/^/     /'
run "PROFILE MATCH (n:\`fish2_Soma\`) WHERE n.\`Forebrain\` = true RETURN count(n);" \
  | grep -E '^(DbHits|Time|Rows)' | sed 's/^/     /'

echo
echo "== summary"
echo "   broken index only : NodeByLabelScan + Filter, ~381,549 DbHits (~2 x node count)"
echo "   correct index     : NodeIndexSeek,                 191 DbHits (matches + 1)"
echo "   ~2000x more reads. Both return the same answer -- only the cost differs."
echo
echo "   Clean up with: ${CLI} rm -f ${NAME}"

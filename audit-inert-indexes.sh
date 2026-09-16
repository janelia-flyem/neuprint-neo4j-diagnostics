#!/bin/bash
#
# Audit a live Neo4j database for inert indexes: those whose label no node
# carries. Such an index is invisible by every other measure -- SHOW INDEXES
# reports state ONLINE and populationPercent 100.0, because an index over a
# label nothing carries is trivially fully populated.
#
# Read-only. Runs two cheap passes: one SHOW INDEXES, then one count-store
# lookup per distinct label. Safe against production.
#
# Works on Neo4j 4.4 and 5.x/CalVer alike -- no APOC, no version-specific
# syntax, nothing written.
#
# Usage:
#   ./audit-inert-indexes.sh                         # local, default db
#   CYPHER_SHELL="docker exec -i neo4j-wasp cypher-shell" ./audit-inert-indexes.sh
#   NEO4J_DB=data ./audit-inert-indexes.sh
#
set -uo pipefail

CS=${CYPHER_SHELL:-cypher-shell}
DB=${NEO4J_DB:-neo4j}
# stdin is redirected from /dev/null: CYPHER_SHELL may be a `docker exec -i`
# wrapper, and without this it would consume the loop's input below, leaving
# one line processed and every count wrong.
q() { ${CS} -d "${DB}" --format plain "$1" < /dev/null 2>/dev/null | tail -n +2 | tr -d '"'; }

if ! q "RETURN 1;" | grep -q 1; then
    echo "ERROR: cannot query database '${DB}' via: ${CS}" 1>&2
    echo "       set CYPHER_SHELL and/or NEO4J_DB (see the header of this script)" 1>&2
    exit 2
fi

echo "Auditing database '${DB}' for indexes on labels no node carries"
echo

# label<TAB>index_count, for node indexes only
COUNTS=$(q "SHOW INDEXES YIELD entityType, labelsOrTypes
            WHERE entityType = 'NODE' AND labelsOrTypes IS NOT NULL
            RETURN labelsOrTypes[0] AS label, count(*) AS n;")

TOTAL_IDX=0; INERT_IDX=0; LIVE_LBL=0; INERT_LBL=0; REPORT=""
while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    lbl="${line%,*}"; n="${line##*,}"
    lbl="$(echo "${lbl}" | sed 's/[[:space:]]*$//')"
    n="$(echo "${n}"   | tr -cd '0-9')"
    [[ -z "${lbl}" || -z "${n}" ]] && continue
    nodes=$(q "MATCH (x:\`${lbl}\`) RETURN count(x);" | tr -cd '0-9')
    TOTAL_IDX=$(( TOTAL_IDX + n ))
    if [[ "${nodes}" == "0" ]]; then
        INERT_IDX=$(( INERT_IDX + n )); INERT_LBL=$(( INERT_LBL + 1 ))
        REPORT="${REPORT}  INERT  ${lbl}  --  ${n} index(es), 0 nodes"$'\n'
    else
        LIVE_LBL=$(( LIVE_LBL + 1 ))
        REPORT="${REPORT}  ok     ${lbl}  --  ${n} index(es), ${nodes} nodes"$'\n'
    fi
done <<< "${COUNTS}"

printf '%s' "${REPORT}" | sort -k1,1r -k2
echo
echo "  node indexes total : ${TOTAL_IDX}"
echo "  on live labels     : $(( TOTAL_IDX - INERT_IDX ))  (${LIVE_LBL} label(s))"
if [[ "${TOTAL_IDX}" -gt 0 ]]; then
    echo "  INERT              : ${INERT_IDX}  (${INERT_LBL} label(s))  = $(( 100 * INERT_IDX / TOTAL_IDX ))% of node indexes"
else
    echo "  INERT              : ${INERT_IDX}"
fi
echo
[[ "${INERT_IDX}" -gt 0 ]] && echo "  An inert index reports ONLINE at 100% while covering nothing." || echo "  No inert indexes found."

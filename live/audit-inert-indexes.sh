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
#   ./audit-inert-indexes.sh                         # local; the server's one user database
#   CYPHER_SHELL="docker exec -i neo4j-wasp cypher-shell" ./audit-inert-indexes.sh
#   NEO4J_DB=data ./audit-inert-indexes.sh           # name it when there are several
#
# Which database: NEO4J_DB if set; otherwise SHOW DATABASES is asked and the
# single non-system database is used. The stores in this fleet are all named
# `data`, not `neo4j`, and auditing an empty `neo4j` database would report
# "nothing to audit" -- a clean result that means nothing. With more than one
# user database the script stops and asks for NEO4J_DB.
#
set -uo pipefail

# Shared palette (COLOR=always|never|auto, NO_COLOR respected).
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../common/colors.sh"

CS=${CYPHER_SHELL:-cypher-shell}
DB=${NEO4J_DB:-}
if [[ -z "${DB}" ]]; then
    # One line per user database, or nothing if cypher-shell cannot connect.
    DBS=$(printf '%s\n' "SHOW DATABASES YIELD name WHERE name <> 'system' RETURN DISTINCT name" \
            | ${CS} -d system --format plain 2>/dev/null | tail -n +2 | tr -d '"' | sed '/^$/d')
    NDB=$(printf '%s' "${DBS}" | grep -c .)
    if [[ "${NDB}" -eq 1 ]]; then
        DB=${DBS}
    elif [[ "${NDB}" -gt 1 ]]; then
        echo "ERROR: ${NDB} user databases ($(echo "${DBS}" | paste -sd, -)); set NEO4J_DB to pick one" 1>&2
        exit 2
    else
        echo "ERROR: could not list databases via: ${CS}" 1>&2
        echo "       set CYPHER_SHELL and/or NEO4J_DB (see the header of this script)" 1>&2
        exit 2
    fi
fi
# The query goes to cypher-shell on stdin, not as an argument: CYPHER_SHELL may
# be a `docker exec -i` or `ssh` wrapper, and a wrapper that rebuilds its
# command line would split a quoted query into words (an ssh wrapper turns the
# `<>` in a WHERE into a redirection). The pipe is also what keeps a `-i`
# wrapper from swallowing the loop's input below, which would leave one line
# processed and every count wrong.
q() { printf '%s\n' "$1" | ${CS} -d "${DB}" --format plain 2>/dev/null | tail -n +2 | tr -d '"'; }

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

# REPORT is built and sorted uncolored -- an escape sequence in the leading
# field would corrupt the sort key -- so the color goes on afterwards, and
# only over text whose visible width does not change.
printf '%s' "${REPORT}" | sort -k1,1r -k2 | sed \
    -e "s/^  INERT /  ${C_RED}INERT${C_RESET} /" \
    -e "s/^  ok    /  ${C_GREEN}ok${C_RESET}    /"
echo
echo "  node indexes total : ${TOTAL_IDX}"
echo "  on live labels     : $(( TOTAL_IDX - INERT_IDX ))  (${LIVE_LBL} label(s))"
if [[ "${INERT_IDX}" -gt 0 ]]; then
    INERT_C="${C_RED}"
else
    INERT_C="${C_GREEN}"
fi
if [[ "${TOTAL_IDX}" -gt 0 ]]; then
    echo "  INERT              : ${INERT_C}${INERT_IDX}${C_RESET}  (${INERT_LBL} label(s))  = ${INERT_C}$(( 100 * INERT_IDX / TOTAL_IDX ))%${C_RESET} of node indexes"
else
    echo "  INERT              : ${INERT_C}${INERT_IDX}${C_RESET}"
fi
echo
[[ "${INERT_IDX}" -gt 0 ]] && echo "  ${C_YELLOW}An inert index reports ONLINE at 100% while covering nothing.${C_RESET}" || echo "  ${C_GREEN}No inert indexes found.${C_RESET}"

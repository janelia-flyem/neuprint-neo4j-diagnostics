#!/bin/bash
#
# Compare the two forms of neuPrintExplorer's buildFastQuery against real data.
#
#   original : WITH textMatches + collect(b) as allMatches, q, user_body
#   fixed    : WITH textMatches, q, user_body, collect(b) as bodyMatches
#              WITH textMatches + bodyMatches as allMatches, q, user_body
#
# The original does not compile from Neo4j 5 onward (error 42I18), so this can
# only compare them on a 4.4 server -- which is exactly where the production
# services still are. On a 5.x+ server the script says so and moves on, which
# makes it double as a survey of which servers have been upgraded.
#
# Equivalence was already established on a synthetic fixture, including 3,000
# rows tied on every sort key. What real data adds is shapes nobody invented:
# apostrophes and unicode in type names, very long strings, and realistic null
# patterns across the eleven searched properties.
#
# Read-only. Two query forms x two bodyId cases per dataset, plus one sample.
#
# Usage:
#   export NEUPRINT_TOKEN=<token>
#   ./compare-fastquery-forms.sh                    # every dataset, all servers
#   ./compare-fastquery-forms.sh https://neuprint.janelia.org   # one server, all
#   ./compare-fastquery-forms.sh https://neuprint-fish2.janelia.org fish2
#   TERMS=lc ./compare-fastquery-forms.sh           # one term: much quicker
#   LIST_ONLY=1 ./compare-fastquery-forms.sh        # plan + version survey only
#
# NOTE ON TOKENS: one token authenticates against all four servers -- they share
# an auth backend. A server that cannot be listed is reported and skipped rather
# than aborting the run.
#
# NOTE ON RUNTIME: a full sweep is 2 + 4 x |TERMS| queries per dataset, and a
# single-letter term matches tens of thousands of neurons on the larger
# datasets. Use TERMS=lc for a fast pass; the default lc,a deliberately
# includes a term broad enough to produce large result sets, since agreement on
# a big sorted set is the interesting evidence.
#
set -uo pipefail

TERMS=${TERMS:-lc,a}
# One neuPrint server can front several Neo4j instances -- neuPrintHTTP's
# MasterDB iterates over MainStores, and /api/custom/custom routes per dataset.
# So datasets on the same hostname may sit on backends of different versions
# (hemibrain:v1.2.1 is 3.5.3 while male-cns:v1.0 is 4.4.16), which is why each
# dataset is probed for its own version rather than assumed to match its host.
SERVERS_DEFAULT="https://neuprint.janelia.org
https://neuprint-pre.janelia.org
https://neuprint-yakuba.janelia.org
https://neuprint-fish2.janelia.org"

if [[ -z "${NEUPRINT_TOKEN:-}" ]]; then
    echo "ERROR: NEUPRINT_TOKEN is not set." 1>&2
    echo "  export NEUPRINT_TOKEN='<token>'   (one token covers all four servers)" 1>&2
    exit 2
fi
command -v python3 > /dev/null || { echo "ERROR: python3 is required." 1>&2; exit 2; }

WORK=$(mktemp -d); trap 'rm -rf "${WORK}"' EXIT

# Datasets to visit, as server|dataset lines. With an explicit dataset, just
# that one; with a server only, every dataset it reports; with no arguments,
# every dataset on every server in SERVERS_DEFAULT.
list_datasets() {  # $1 = server
    curl -sS -m 120 "$1/api/dbmeta/datasets" \
        -H "Authorization: Bearer ${NEUPRINT_TOKEN}" 2>/dev/null \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(d, dict) or d.get('error') or not d:
    sys.exit(1)
print('\n'.join(sorted(d.keys())))
"
}

if [[ $# -ge 2 ]]; then
    PAIRS="${1%/}|$2"
else
    if [[ $# -eq 1 ]]; then SERVERS="${1%/}"; else SERVERS="${SERVERS_DEFAULT}"; fi
    PAIRS=""
    while IFS= read -r srv; do
        [[ -z "${srv}" ]] && continue
        srv="${srv%/}"
        if ! ds=$(list_datasets "${srv}"); then
            echo "WARNING: could not list datasets on ${srv} -- skipping it" 1>&2
            continue
        fi
        n=$(echo "${ds}" | grep -c .)
        echo "  ${srv}: ${n} dataset(s)" 1>&2
        while IFS= read -r one; do
            [[ -n "${one}" ]] && PAIRS+="${srv}|${one}"$'\n'
        done <<< "${ds}"
    done <<< "${SERVERS}"
    if [[ -z "${PAIRS}" ]]; then
        echo "ERROR: no datasets found on any server. Check the token." 1>&2
        exit 1
    fi
    nterms=$(( $(echo "${TERMS}" | tr ',' '\n' | grep -c .) ))
    echo "  total: $(echo "${PAIRS}" | grep -c .) dataset(s), TERMS='${TERMS}'," \
         "$(( 2 + 4 * nterms )) queries each" 1>&2
fi


# The full query, parameterised by term, bodyId and which WITH form to use.
# Only bodyId/priority/type_priority are returned, not all fourteen columns:
# every other column is a plain property of n, so if the set of
# (bodyId, priority, type_priority) agrees -- and DISTINCT guarantees one row
# per node -- the full rows necessarily agree too. It keeps responses small on
# datasets where a short term matches tens of thousands of neurons.
build_query() {  # $1=term $2=bodyId $3=orig|fixed
    local mid
    if [[ "$3" == "orig" ]]; then
        mid="WITH textMatches + collect(b) as allMatches, q, user_body"
    else
        mid="WITH textMatches, q, user_body, collect(b) as bodyMatches WITH textMatches + bodyMatches as allMatches, q, user_body"
    fi
    cat <<EOF
WITH toLower('$1') as q, $2 as user_body
CALL { WITH q CALL db.index.fulltext.queryNodes('find_neurons_fulltext_properties_index', '*' + q + '*') YIELD node as n RETURN collect(n) as textMatches }
OPTIONAL MATCH (b:Neuron) WHERE user_body <> 0 AND b.bodyId = user_body
${mid}
UNWIND allMatches as n
WITH DISTINCT n, q, user_body, [toLower(n.type), toLower(n.instance), toLower(n.hemibrainType), toLower(n.flywireType), toLower(n.systematicType), toLower(n.itoleeHl), toLower(n.trumanHl), toLower(n.synonyms), toLower(n.class), toLower(n.entryNerve), toLower(n.exitNerve)] as props
WITH n, q, props, user_body, CASE WHEN n.bodyId = user_body AND user_body <> 0 THEN 0 WHEN any(p IN props WHERE p = q) THEN 1 WHEN any(p IN props WHERE p STARTS WITH q) THEN 2 WHEN any(p IN props WHERE p STARTS WITH '(' + q) THEN 3 WHEN any(p IN props WHERE p CONTAINS q) THEN 4 ELSE 5 END as priority, CASE WHEN toLower(n.type) STARTS WITH q THEN 0 WHEN toLower(n.type) CONTAINS q THEN 1 ELSE 2 END as type_priority
RETURN toString(n.bodyId) as bodyId, priority, type_priority
ORDER BY priority, type_priority, n.type, n.instance
EOF
}

post() {  # $1=server $2=dataset $3=cypher-file -> response on stdout
    python3 - "$2" "$3" > "${WORK}/payload.json" <<'PY'
import json, sys
print(json.dumps({"dataset": sys.argv[1], "cypher": open(sys.argv[2]).read()}))
PY
    curl -sS -m 300 -X POST "$1/api/custom/custom" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer ${NEUPRINT_TOKEN}" \
        -d @"${WORK}/payload.json"
}

# Classify a response: DATA / ERROR:<msg> / NOCOMPILE
classify() {  # $1=response file
    python3 - "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"ERROR:response was not JSON ({str(e)[:60]})"); sys.exit(0)
if isinstance(d, dict) and d.get('error'):
    e = d['error']
    if '42I18' in e or 'non-grouping' in e or 'implicit grouping' in e:
        print("NOCOMPILE"); sys.exit(0)
    # Two error families dominate on this fleet, and both mean "the fast query
    # cannot run here" rather than "the two forms disagree". Named, so the
    # per-dataset table stays readable; the full text is printed once per
    # dataset by full_error().
    if 'queryNodes' in e and 'ProcedureCallFailed' in e:
        print("ERROR:fulltext query failed"); sys.exit(0)
    if 'SyntaxError' in e and ("Invalid input '{'" in e or 'CALL {' in e):
        print("ERROR:no CALL {} subqueries"); sys.exit(0)
    print(f"ERROR:{e[:160]}"); sys.exit(0)
if isinstance(d, dict) and 'data' in d:
    print("DATA"); sys.exit(0)
print(f"ERROR:unexpected shape ({str(d)[:100]})")
PY
}

# The untruncated server error, printed once per dataset. classify() collapses
# errors into short names for the table; this is what actually diagnoses them.
full_error() {  # $1=response file
    python3 - "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("      (response was not JSON)"); sys.exit(0)
e = d.get('error') if isinstance(d, dict) else None
if e:
    for i in range(0, len(e), 100):
        print(f"      {e[i:i+100]}")
PY
}

rows_sorted() { python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))['data']
print('\n'.join(sorted('|'.join(str(c) for c in r) for r in d)))
" "$1"; }
rows_ordered() { python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))['data']
print('\n'.join('|'.join(str(c) for c in r) for r in d))
" "$1"; }

# The backend version, which predicts everything else: the original form only
# compiles on 4.4 and earlier, and 3.5 cannot run the fast query in either form
# (no CALL {} subqueries). Asked per dataset rather than per server, because one
# hostname can front backends of different versions -- hemibrain:v1.2.1 is 3.5.3
# while male-cns:v1.0 on the same host is 4.4.16.
probe_version() {  # $1=server $2=dataset -> version string on stdout
    echo "CALL dbms.components() YIELD versions, edition RETURN versions[0] AS v, edition LIMIT 1" \
        > "${WORK}/v.cypher"
    post "$1" "$2" "${WORK}/v.cypher" > "${WORK}/v.json"
    if [[ "$(classify "${WORK}/v.json")" == "DATA" ]]; then
        python3 -c "
import json
d = json.load(open('${WORK}/v.json'))['data']
print(' '.join(str(c) for c in d[0]) if d else 'unknown')"
    else
        echo "unreachable"
    fi
}

# Whether the fulltext index the fast query names actually exists. The fast
# query calls db.index.fulltext.queryNodes on a hardcoded index name, so a
# dataset without that index cannot serve the query at all -- it errors rather
# than returning fewer rows. SHOW INDEXES is 4.x+, so 3.5 is reported as n/a.
FULLTEXT_INDEX=find_neurons_fulltext_properties_index
probe_fulltext() {  # $1=server $2=dataset $3=version -> status on stdout
    case "$3" in
        3.*)                 echo "n/a (3.5)"; return ;;
        unreachable|unknown) echo "-";         return ;;
    esac
    echo "SHOW INDEXES YIELD name, type, state WHERE type = 'FULLTEXT' RETURN name, state" \
        > "${WORK}/fx.cypher"
    post "$1" "$2" "${WORK}/fx.cypher" > "${WORK}/fx.json"
    if [[ "$(classify "${WORK}/fx.json")" != "DATA" ]]; then
        echo "probe failed"; return
    fi
    python3 -c "
import json
d = json.load(open('${WORK}/fx.json'))['data']
want = '${FULLTEXT_INDEX}'
found = {r[0]: r[1] for r in d}
if want in found:
    print('yes' if found[want] == 'ONLINE' else 'yes but ' + str(found[want]))
elif found:
    print('MISSING (fulltext present: ' + ','.join(sorted(found)) + ')')
else:
    print('MISSING (no fulltext index at all)')"
}

if [[ "${LIST_ONLY:-0}" == "1" ]]; then
    echo
    printf "  %-16s %-22s %-18s %s\n" "SERVER" "DATASET" "NEO4J" "FULLTEXT INDEX"
    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        server=${line%%|*}; dataset=${line#*|}
        host=${server#https://}; host=${host%%.*}
        v=$(probe_version  "${server}" "${dataset}"      < /dev/null)
        f=$(probe_fulltext "${server}" "${dataset}" "${v}" < /dev/null)
        printf "  %-16s %-22s %-18s %s\n" "${host}" "${dataset}" "${v}" "${f}"
    done <<< "${PAIRS}"
    echo
    echo "  A dataset needs 4.4+ (for CALL {}) AND the fulltext index above"
    echo "  before neuPrintExplorer's fast query can run against it at all."
    exit 0
fi

TOTAL=0; AGREE=0; DISAGREE=0; SKIPPED=0
DS_OK=0; DS_BAD=0; DS_NOFT=0; DS_NOSUB=0; DS_OTHER=0
: > "${WORK}/table.tsv"
row() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "${WORK}/table.tsv"; }

while IFS='|' read -r server dataset; do
    [[ -z "${server}" ]] && continue
    host=${server#https://}; host=${host%%.*}
    echo
    echo "=============================================================="
    echo " ${dataset}  @  ${server}"
    echo "=============================================================="

    VERSION=$(probe_version "${server}" "${dataset}" < /dev/null)
    echo "  neo4j version  : ${VERSION}"

    DS_ID=0; DS_SET=0; DS_DIFF=0; DS_ERR=0; DS_ROWS=0; DS_NOTE=""

    # A real bodyId, so the collect(b) path is genuinely exercised.
    echo "MATCH (n:Neuron) WHERE n.bodyId IS NOT NULL RETURN n.bodyId AS b ORDER BY n.bodyId LIMIT 1" > "${WORK}/s.cypher"
    post "${server}" "${dataset}" "${WORK}/s.cypher" > "${WORK}/s.json"
    st=$(classify "${WORK}/s.json")
    if [[ "${st}" != "DATA" ]]; then
        echo "  SKIPPED -- ${st#ERROR:}"
        SKIPPED=$(( SKIPPED + 1 ))
        row "${host}" "${dataset}" "${VERSION}" "skipped: ${st#ERROR:}"
        continue
    fi
    SAMPLE=$(python3 -c "
import json,sys
d=json.load(open('${WORK}/s.json'))['data']
print(d[0][0] if d else 0)")
    echo "  sampled bodyId: ${SAMPLE}"

    IFS=',' read -ra TERM_LIST <<< "${TERMS}"
    for term in "${TERM_LIST[@]}"; do
        for body in 0 "${SAMPLE}"; do
            TOTAL=$(( TOTAL + 1 ))
            build_query "${term}" "${body}" orig  > "${WORK}/o.cypher"
            build_query "${term}" "${body}" fixed > "${WORK}/f.cypher"
            post "${server}" "${dataset}" "${WORK}/o.cypher" > "${WORK}/o.json"
            post "${server}" "${dataset}" "${WORK}/f.cypher" > "${WORK}/f.json"
            so=$(classify "${WORK}/o.json"); sf=$(classify "${WORK}/f.json")

            label=$(printf "term='%s' bodyId=%s" "${term}" "${body}")
            if [[ "${so}" == "NOCOMPILE" ]]; then
                printf "  %-30s original does not compile -- server is 5.x or later\n" "${label}"
                SKIPPED=$(( SKIPPED + 1 )); TOTAL=$(( TOTAL - 1 ))
                DS_NOTE="original will not compile (5.x+)"; break 2
            fi
            if [[ "${so}" != "DATA" || "${sf}" != "DATA" ]]; then
                printf "  %-30s ERROR  orig=%s fixed=%s\n" "${label}" "${so#ERROR:}" "${sf#ERROR:}"
                # Print the untruncated error once, on the first failure for
                # this dataset -- the short name above is for the table.
                if [[ "${DS_ERR}" -eq 0 ]]; then full_error "${WORK}/o.json"; fi
                SKIPPED=$(( SKIPPED + 1 )); TOTAL=$(( TOTAL - 1 ))
                DS_ERR=$(( DS_ERR + 1 ))
                [[ -z "${DS_NOTE}" ]] && DS_NOTE="${so#ERROR:}"
                continue
            fi

            n=$(python3 -c "import json;print(len(json.load(open('${WORK}/o.json'))['data']))")
            [[ "${n}" -gt "${DS_ROWS}" ]] && DS_ROWS=${n}
            if [[ "$(rows_sorted "${WORK}/o.json")" == "$(rows_sorted "${WORK}/f.json")" ]]; then
                AGREE=$(( AGREE + 1 ))
                if [[ "$(rows_ordered "${WORK}/o.json")" == "$(rows_ordered "${WORK}/f.json")" ]]; then
                    printf "  %-30s IDENTICAL  %s rows (same order too)\n" "${label}" "${n}"
                    DS_ID=$(( DS_ID + 1 ))
                else
                    printf "  %-30s SAME SET   %s rows (tie order differs -- both valid)\n" "${label}" "${n}"
                    DS_SET=$(( DS_SET + 1 ))
                fi
            else
                DISAGREE=$(( DISAGREE + 1 )); DS_DIFF=$(( DS_DIFF + 1 ))
                printf "  %-30s *** DIFFER ***  orig=%s rows fixed=%s rows\n" "${label}" \
                    "${n}" "$(python3 -c "import json;print(len(json.load(open('${WORK}/f.json'))['data']))")"
                python3 - "${WORK}/o.json" "${WORK}/f.json" <<'PY'
import json, sys
a = {'|'.join(map(str, r)) for r in json.load(open(sys.argv[1]))['data']}
b = {'|'.join(map(str, r)) for r in json.load(open(sys.argv[2]))['data']}
for tag, rows in (("only in original", a - b), ("only in fixed", b - a)):
    if rows:
        print(f"      {tag}: {len(rows)}")
        for r in sorted(rows)[:5]: print(f"        {r}")
PY
            fi
        done
    done

    if [[ "${DS_DIFF}" -gt 0 ]]; then
        verdict="DIFFER on ${DS_DIFF}"
        DS_BAD=$(( DS_BAD + 1 ))
    elif [[ $(( DS_ID + DS_SET )) -gt 0 ]]; then
        verdict=$(printf "%d equivalent, max %s rows" "$(( DS_ID + DS_SET ))" "${DS_ROWS}")
        [[ "${DS_SET}" -gt 0 ]] && verdict="${verdict} (${DS_SET} tie-order only)"
        DS_OK=$(( DS_OK + 1 ))
    else
        verdict="not compared: ${DS_NOTE:-no result}"
        case "${DS_NOTE}" in
            "fulltext query failed")  DS_NOFT=$(( DS_NOFT + 1 )) ;;
            "no CALL {} subqueries")  DS_NOSUB=$(( DS_NOSUB + 1 )) ;;
            *)                        DS_OTHER=$(( DS_OTHER + 1 )) ;;
        esac
    fi
    row "${host}" "${dataset}" "${VERSION}" "${verdict}"
done <<< "${PAIRS}"

echo
echo "=============================================================="
echo " Per-dataset results"
echo "=============================================================="
if [[ -s "${WORK}/table.tsv" ]]; then
    { printf 'SERVER\tDATASET\tNEO4J\tRESULT\n'; cat "${WORK}/table.tsv"; } \
        | awk -F'\t' '{printf "  %-16s %-22s %-18s %s\n", $1, $2, $3, $4}'
fi

echo
echo "=============================================================="
echo " Summary"
echo "=============================================================="
echo "  datasets visited        : $(( DS_OK + DS_BAD + DS_NOFT + DS_NOSUB + DS_OTHER ))"
echo "    compared              : ${DS_OK}"
echo "    can't: fulltext index : ${DS_NOFT}"
echo "    can't: neo4j 3.5      : ${DS_NOSUB}"
echo "    can't: other          : ${DS_OTHER}"
echo
echo "  comparisons run : ${TOTAL}"
echo "  equivalent      : ${AGREE}"
echo "  differing       : ${DISAGREE}"
echo "  skipped         : ${SKIPPED}"
echo
if [[ "${DISAGREE}" -gt 0 ]]; then
    echo "  The two forms are NOT equivalent on real data. Do not propose the fix"
    echo "  until the difference above is understood."
    exit 1
elif [[ "${AGREE}" -eq 0 ]]; then
    echo "  Nothing was compared. Check that the token is valid -- every dataset"
    echo "  was enumerated from /api/dbmeta/datasets, so the names are not the"
    echo "  problem."
    exit 1
else
    echo "  The fix returned the same rows as the original everywhere it could be"
    echo "  compared. Note this only ever tests 4.4 servers: the original cannot"
    echo "  run on 5.x or later to be compared against."
fi

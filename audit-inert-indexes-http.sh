#!/bin/bash
#
# Audit neuPrint datasets for inert indexes -- those whose label no node
# carries -- over the HTTPS API, without Bolt or container access.
#
# An inert index is invisible by every other measure: SHOW INDEXES reports
# state ONLINE and populationPercent 100.0, because an index over a label
# nothing carries is trivially fully populated. The only way to detect one is
# to ask whether any node actually carries the label.
#
# Read-only: two queries per dataset, no writes. The second uses count-store
# lookups (1 DbHit per label), so it is instant regardless of database size.
#
# Companion to audit-inert-indexes.sh, which does the same over Bolt via
# cypher-shell -- prefer that if you have container or host access.
#
# Usage:
#   export NEUPRINT_TOKEN=<your token>
#   ./audit-inert-indexes-http.sh https://neuprint-fish2.janelia.org fish2
#   ./audit-inert-indexes-http.sh https://neuprint.janelia.org      # all datasets
#
set -uo pipefail

SERVER=${1:-}
DATASET=${2:-}

if [[ -z "${NEUPRINT_TOKEN:-}" ]]; then
    cat <<'MSG' 1>&2
ERROR: NEUPRINT_TOKEN is not set.

  Get a token from your neuPrint server's account page, then:

    export NEUPRINT_TOKEN='<token>'

  It is a bearer credential -- prefer exporting it in your shell over pasting
  it into a command line, which lands in your shell history.
MSG
    exit 2
fi

if [[ -z "${SERVER}" ]]; then
    echo "Usage: $(basename "$0") <server-url> [dataset]" 1>&2
    echo "  With no dataset, every dataset on the server is audited in sequence." 1>&2
    echo "  e.g. $(basename "$0") https://neuprint-fish2.janelia.org fish2" 1>&2
    exit 2
fi

command -v python3 > /dev/null || { echo "ERROR: python3 is required." 1>&2; exit 2; }

SERVER="${SERVER%/}"
WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

# Note: no "version" field is sent. That parameter is checked against the
# *data model* version, not the dataset version, and supplying a dataset
# version there fails with "neo4j data model version incompatible".
post() {  # $1 = json payload file
    curl -sS -X POST "${SERVER}/api/custom/custom" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer ${NEUPRINT_TOKEN}" \
        -d @"$1"
}

api_error() {  # $1 = response file; prints the problem, returns 1 if bad
    python3 - "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"response was not JSON: {e}"); sys.exit(1)
if isinstance(d, dict) and d.get('error'):
    print(f"API error: {d['error']}"); sys.exit(1)
if not (isinstance(d, dict) and 'data' in d):
    print(f"unexpected response shape: {str(d)[:200]}"); sys.exit(1)
PY
}

# Audit one dataset. Prints its table; appends "<dataset> <total> <inert>" to
# the tally file. Returns 1 if the dataset could not be audited at all.
audit_one() {
    local ds="$1" pfx="${WORK}/$(echo "$1" | tr -c 'A-Za-z0-9' '_')"

    cat > "${pfx}.q1" <<JSON
{"dataset": "${ds}",
 "cypher": "SHOW INDEXES YIELD entityType, labelsOrTypes WHERE entityType = 'NODE' AND labelsOrTypes IS NOT NULL RETURN labelsOrTypes[0] AS label, count(*) AS indexes ORDER BY label"}
JSON
    post "${pfx}.q1" > "${pfx}.r1"
    local err
    if ! err=$(api_error "${pfx}.r1"); then
        echo "  SKIPPED -- ${err}"
        return 1
    fi

    # One CALL subquery per label. The subquery form matters: a bare
    # "RETURN 'label', count(n)" makes the literal a grouping key, and with
    # zero matching nodes there are no groups -- so an empty label would emit
    # no row at all rather than a zero, and inert indexes would silently
    # vanish from the output instead of standing out.
    python3 - "${pfx}.r1" "${ds}" > "${pfx}.q2" 2> "${pfx}.err" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))['data']
if not rows:
    print("NO_INDEXES", file=sys.stderr); sys.exit(3)
parts = []
for label, n in rows:
    if '`' in label:            # would break the backtick quoting
        print(f"SKIP_BACKTICK:{label}", file=sys.stderr); continue
    parts.append(
        "CALL { MATCH (n:`%s`) RETURN count(n) AS c } "
        "RETURN '%s' AS label, %d AS indexes, c AS nodes" % (label, label.replace("'", "\\'"), n)
    )
print(json.dumps({"dataset": sys.argv[2], "cypher": " UNION ALL ".join(parts)}))
PY
    local rc=$?
    if grep -q '^SKIP_BACKTICK:' "${pfx}.err" 2>/dev/null; then
        sed -n 's/^SKIP_BACKTICK:/  WARNING: skipped label containing a backtick: /p' "${pfx}.err"
    fi
    if [[ "${rc}" -eq 3 ]]; then
        echo "  no node indexes -- nothing to audit"
        echo "${ds} 0 0" >> "${WORK}/tally"
        return 0
    elif [[ "${rc}" -ne 0 ]]; then
        echo "  SKIPPED -- could not build the node-count query"
        grep -v '^SKIP_BACKTICK:' "${pfx}.err" | sed 's/^/    /'
        return 1
    fi

    post "${pfx}.q2" > "${pfx}.r2"
    if ! err=$(api_error "${pfx}.r2"); then
        echo "  SKIPPED -- ${err}"
        return 1
    fi

    python3 - "${pfx}.r2" "${ds}" "${WORK}/tally" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))['data']
total = sum(r[1] for r in rows)
inert = sum(r[1] for r in rows if r[2] == 0)
for label, idx, nodes in sorted(rows, key=lambda r: (r[2] != 0, r[0])):
    tag = "INERT" if nodes == 0 else "ok   "
    print(f"  {tag}  {label:<34}{idx:>5} index(es){nodes:>14,} nodes")
print(f"\n  node indexes {total}, on live labels {total - inert}, "
      f"INERT {inert}" + (f" = {100 * inert // total}%" if total else ""))
open(sys.argv[3], 'a').write(f"{sys.argv[2]} {total} {inert}\n")
PY
}

# --- work out which datasets to audit ---------------------------------------
if [[ -n "${DATASET}" ]]; then
    DATASETS="${DATASET}"
else
    echo "No dataset given -- enumerating datasets on ${SERVER}"
    curl -sS "${SERVER}/api/dbmeta/datasets" \
        -H "Authorization: Bearer ${NEUPRINT_TOKEN}" > "${WORK}/ds.json"
    # The endpoint returns an object keyed by dataset name.
    DATASETS=$(python3 - "${WORK}/ds.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"could not parse the dataset list: {e}", file=sys.stderr); sys.exit(1)
if isinstance(d, dict) and d.get('error'):
    print(f"API error: {d['error']}", file=sys.stderr); sys.exit(1)
if not isinstance(d, dict) or not d:
    print(f"unexpected dataset list: {str(d)[:200]}", file=sys.stderr); sys.exit(1)
print("\n".join(sorted(d.keys())))
PY
    ) || { echo "ERROR: could not list datasets." 1>&2; exit 1; }
    echo "  found: $(echo "${DATASETS}" | tr '\n' ' ')"
fi

# --- audit each in sequence --------------------------------------------------
FAILED=0
while IFS= read -r ds; do
    [[ -z "${ds}" ]] && continue
    echo
    echo "=============================================================="
    echo " ${ds}"
    echo "=============================================================="
    audit_one "${ds}" || FAILED=$(( FAILED + 1 ))
done <<< "${DATASETS}"

# --- summary, only worth printing for more than one dataset -----------------
if [[ "$(echo "${DATASETS}" | grep -c .)" -gt 1 ]]; then
    echo
    echo "=============================================================="
    echo " Summary"
    echo "=============================================================="
    if [[ -s "${WORK}/tally" ]]; then
        python3 - "${WORK}/tally" <<'PY'
import sys
rows = [l.split() for l in open(sys.argv[1]) if l.strip()]
w = max(len(r[0]) for r in rows)
gt = gi = 0
for ds, total, inert in rows:
    total, inert = int(total), int(inert)
    gt += total; gi += inert
    pct = f"{100 * inert // total}%" if total else "-"
    flag = "  <-- inert indexes present" if inert else ""
    print(f"  {ds:<{w}}  {total:>5} node indexes  {inert:>5} inert  {pct:>5}{flag}")
print(f"\n  across {len(rows)} dataset(s): {gt} node indexes, {gi} inert"
      + (f" = {100 * gi // gt}%" if gt else ""))
PY
    else
        echo "  no datasets could be audited"
    fi
fi

[[ "${FAILED}" -gt 0 ]] && { echo; echo "  ${FAILED} dataset(s) could not be audited (see above)."; }
exit 0

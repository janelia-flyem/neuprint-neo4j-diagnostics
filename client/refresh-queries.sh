#!/usr/bin/env bash
#
# Re-extract the FindNeurons queries from a neuPrintExplorer checkout into
# queries/, so that what gets tested is what the component actually sends
# rather than a transcription of it.
#
#   ./refresh-queries.sh [path-to-neuPrintExplorer] [git-ref]
#
# Defaults to the local checkout at origin/master. Pass a ref to pin the
# queries to the release you are rolling out, e.g.
#
#   ./refresh-queries.sh ~/Documents/workspace/Git/neuPrintExplorer v1.72.3
#
# The queries carry placeholders that check-rollout.py substitutes:
#   TERM    the search term, escaped for a Cypher literal
#   BODY    the bodyId
#   LUCENE  the fulltext query string built by buildLuceneQuery
#
# This wraps extract-queries.sh from the neuprint-neo4j-diagnostics repo,
# which is the single implementation of that parsing. If that repo moves, set
# DIAGNOSTICS to its findneurons/ directory.
set -euo pipefail

# Shared palette (COLOR=always|never|auto, NO_COLOR respected).
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../common/colors.sh"

REPO=${1:-$HOME/Documents/workspace/Git/neuPrintExplorer}
REF=${2:-origin/master}
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# findneurons/ is a sibling directory in this same repo now, so the default
# needs no absolute path. DIAGNOSTICS still overrides it if that moves.
DIAGNOSTICS=${DIAGNOSTICS:-${HERE}/../findneurons}

if [[ ! -x "${DIAGNOSTICS}/extract-queries.sh" ]]; then
    echo "${C_RED}ERROR${C_RESET}: ${DIAGNOSTICS}/extract-queries.sh not found." 1>&2
    echo "It should be findneurons/extract-queries.sh in this repo." 1>&2
    exit 2
fi

bash "${DIAGNOSTICS}/extract-queries.sh" "${REPO}" "${REF}" "${HERE}/queries"

# extract-queries.sh ships a copy of its lucene.py alongside the queries, but
# check-rollout.py implements that rule itself, so drop it rather than leave
# two copies to drift apart.
rm -f "${HERE}/queries/lucene.py"

# Record where these came from. The README stresses that which ref you
# extracted decides whose behavior gets measured, so don't leave that to
# memory -- check-rollout.py prints this line in every report.
RESOLVED=$(git -C "${REPO}" rev-parse --short "${REF}" 2>/dev/null || echo unknown)
printf '%s @ %s (%s), extracted %s\n' \
    "$(basename "${REPO}")" "${REF}" "${RESOLVED}" "$(date +%Y-%m-%d)" \
    > "${HERE}/queries/SOURCE"
echo "  SOURCE  $(cat "${HERE}/queries/SOURCE")"

echo
echo "Queries in ${HERE}/queries now come from ${REF}."
echo "Record that ref in the rollout notes -- the capability check has changed"
echo "twice, so which build you extracted decides whose behavior is tested."

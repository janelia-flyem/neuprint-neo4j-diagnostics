#!/usr/bin/env bash
#
# Pull the FindNeurons search and capability queries out of
# NeuronInputField.jsx, so that the other scripts here test what the component
# actually sends rather than a transcription of it.
#
#   ./extract-queries.sh <path-to-neuPrintExplorer> [git-ref] [outdir]
#
#   path      a checkout of connectome-neuprint/neuPrintExplorer
#   git-ref   which version to read; default HEAD. Use a ref to audit the
#             deployed client, e.g. the commit before the coverage check.
#   outdir    where to write the .cypher files; default ./queries
#
# Writes: fast.cypher slow.cypher version.cypher index.cypher
# The search queries contain the placeholders TERM and BODY for the callers to
# substitute.
#
# Why a git-ref matters: the capability check has changed twice, and what a
# server is asked depends on which build is deployed. Extracting per ref is how
# you compare "what users get today" against "what this branch would do".
set -uo pipefail

REPO=${1:?usage: extract-queries.sh <path-to-neuPrintExplorer> [git-ref] [outdir]}
REF=${2:-HEAD}
OUT=${3:-./queries}
SRC=src/js/plugins/query/shared/NeuronInputField.jsx

command -v node > /dev/null || { echo "ERROR: node is required" 1>&2; exit 2; }
[ -d "$REPO/.git" ] || { echo "ERROR: $REPO is not a git checkout" 1>&2; exit 2; }

mkdir -p "$OUT"
JSX="$(mktemp)"; trap 'rm -f "$JSX"' EXIT
if ! git -C "$REPO" show "${REF}:${SRC}" > "$JSX" 2>/dev/null; then
    echo "ERROR: cannot read ${SRC} at ${REF} in ${REPO}" 1>&2
    exit 1
fi

# Each query is the first template literal after its declaration. The property
# names inside the search queries are left exactly as written -- their text has
# been verified equivalent against production data, so it must not be rebuilt.
node --input-type=module -e "
import fs from 'fs';
const src = fs.readFileSync(process.argv[1], 'utf8');
const out = process.argv[2];
const grab = (marker) => {
  const i = src.indexOf(marker);
  if (i < 0) return null;
  const a = src.indexOf('\`', i), b = src.indexOf('\`', a + 1);
  return src.slice(a + 1, b);
};
const NAME = 'find_neurons_fulltext_properties_index';
// The client has wrapped the term in helpers over time. Map every form it
// has used onto the placeholders the other scripts substitute:
//   TERM    the raw search term, for the Cypher literals
//   BODY    the bodyId
//   LUCENE  the fulltext query string, which since v1.72.3 is built by
//           buildLuceneQuery rather than being '*' + q + '*'
// Older revisions have no LUCENE placeholder; they build it inline in Cypher,
// so the substitution is simply a no-op there.
const subst = (s) => s === null ? null : s
  .replace(/\\\$\{buildLuceneQuery\(inputValue\)\}/g, 'LUCENE')
  .replace(/\\\$\{escapeForCypher\(inputValue\)\}/g, 'TERM')
  .replace(/\\\$\{inputValue\}/g, 'TERM')
  .replace(/\\\$\{bodyId\}/g, 'BODY')
  .replace(/\\\$\{FULLTEXT_INDEX_NAME\}/g, NAME);
const wanted = {
  'fast.cypher':    'function buildFastQuery',
  'slow.cypher':    'function buildSlowQuery',
  'version.cypher': 'const versionCypher',
  'index.cypher':   'const indexCypher',
};
let missing = [];
for (const [file, marker] of Object.entries(wanted)) {
  const q = subst(grab(marker));
  if (q === null) { missing.push(file + ' (' + marker + ')'); continue; }
  fs.writeFileSync(out + '/' + file, q);
  console.log('  ' + file + '  ' + q.split('\n').length + ' lines');
}
if (missing.length) {
  console.error('ERROR: not found in this revision: ' + missing.join(', '));
  process.exit(1);
}
" "$JSX" "$OUT" || exit 1

# Ship the token rule alongside the queries so the consuming scripts have a
# single implementation to fill the LUCENE placeholder with.
cp "$(dirname "$0")/lucene.py" "$OUT/lucene.py" 2>/dev/null || true
chmod +x "$OUT/lucene.py" 2>/dev/null || true

echo "extracted from ${REF} into ${OUT}"

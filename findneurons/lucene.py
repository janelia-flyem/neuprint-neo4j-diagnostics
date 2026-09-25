#!/usr/bin/env python3
"""Build the fulltext query string for a search term.

Mirrors buildLuceneQuery in NeuronInputField.jsx. The index analyzer splits
on punctuation, so "R1-R6" is stored as the tokens r1 and r6 and a wildcard
search for the whole string matches nothing; the term is split the same way
and every token required.

Written here by extract-queries.sh so the consuming scripts share one copy.
Revisions before v1.72.3 build the query inline in Cypher and never call this.

    ./lucene.py 'R1-R6'   ->   *r1* AND *r6*
"""
import re
import sys

def lucene_query(term):
    tokens = [t for t in re.split(r'[^a-z0-9_]+', str(term).lower()) if t]
    if not tokens:
        return '*'
    return ' AND '.join('*%s*' % t for t in tokens)

if __name__ == '__main__':
    print(lucene_query(sys.argv[1] if len(sys.argv) > 1 else ''))

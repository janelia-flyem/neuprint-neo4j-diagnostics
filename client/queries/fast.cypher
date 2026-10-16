WITH toLower('TERM') as q, BODY as user_body

// Full-text search wrapped in subquery to preserve pipeline when no results
CALL {
  WITH q
  CALL db.index.fulltext.queryNodes('find_neurons_fulltext_properties_index', 'LUCENE')
  YIELD node as n
  RETURN collect(n) as textMatches
}

// Add bodyId match if specified.
//
// The aggregation is deliberately split across two WITH clauses. Combining
// collect(b) with the 'textMatches + ...' expression in a single clause makes
// textMatches an implicit grouping key; Cypher 4.4 accepted that, and Cypher 5
// rejects it (42I18). Splitting it aggregates first, then adds the lists, which
// both language versions accept.
OPTIONAL MATCH (b:Neuron) WHERE user_body <> 0 AND b.bodyId = user_body
WITH textMatches, q, user_body, collect(b) as bodyMatches
WITH textMatches + bodyMatches as allMatches, q, user_body
UNWIND allMatches as n

WITH DISTINCT n, q, user_body,
     [toLower(n.type), toLower(n.instance), toLower(n.hemibrainType),
      toLower(n.flywireType), toLower(n.systematicType), toLower(n.itoleeHl),
      toLower(n.trumanHl), toLower(n.synonyms), toLower(n.class),
      toLower(n.entryNerve), toLower(n.exitNerve)] as props
WITH n, q, props, user_body,
     CASE
         WHEN n.bodyId = user_body AND user_body <> 0 THEN 0
         WHEN any(p IN props WHERE p = q) THEN 1
         WHEN any(p IN props WHERE p STARTS WITH q) THEN 2
         WHEN any(p IN props WHERE p STARTS WITH '(' + q) THEN 3
         WHEN any(p IN props WHERE p CONTAINS q) THEN 4
         ELSE 5
     END as priority,
     CASE
         WHEN toLower(n.type) STARTS WITH q THEN 0
         WHEN toLower(n.type) CONTAINS q THEN 1
         ELSE 2
     END as type_priority
RETURN
    toString(n.bodyId) as bodyId,
    n.type as type,
    n.instance as instance,
    n.hemibrainType as hemibrainType,
    n.flywireType as flywireType,
    n.systematicType as systematicType,
    n.itoleeHl as itoLeeHl,
    n.trumanHl as trumanHl,
    n.synonyms as synonyms,
    n.class as class,
    n.entryNerve as entryNerve,
    n.exitNerve as exitNerve,
    priority,
    type_priority
ORDER BY priority, type_priority, n.type, n.instance
WITH toLower('TERM') as q, BODY as user_body, '(' + toLower('TERM') as parenQ
MATCH (n:Neuron)
WHERE n.bodyId = user_body
   OR any(prop IN [
       n.type, n.instance, n.hemibrainType, n.flywireType,
       n.systematicType, n.itoleeHl, n.trumanHl, n.synonyms,
       n.class, n.entryNerve, n.exitNerve
   ] WHERE toLower(prop) CONTAINS q)
WITH n, q, parenQ, user_body,
     [toLower(n.type), toLower(n.instance), toLower(n.hemibrainType),
      toLower(n.flywireType), toLower(n.systematicType), toLower(n.itoleeHl),
      toLower(n.trumanHl), toLower(n.synonyms), toLower(n.class),
      toLower(n.entryNerve), toLower(n.exitNerve)] as props
WITH n, q, parenQ, props, user_body,
     CASE
         WHEN n.bodyId = user_body AND user_body <> 0 THEN 0
         WHEN any(p IN props WHERE p = q) THEN 1
         WHEN any(p IN props WHERE p STARTS WITH q) THEN 2
         WHEN any(p IN props WHERE p STARTS WITH parenQ) THEN 3
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
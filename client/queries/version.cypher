CALL dbms.components() YIELD name, versions
WITH name, versions WHERE name = 'Neo4j Kernel'
RETURN versions[0] as version
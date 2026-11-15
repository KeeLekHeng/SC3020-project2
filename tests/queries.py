"""Queries the forcing tests run: TPC-H Q1-Q22 and shapes that broke forcing before."""

from pathlib import Path

# TPC-H Q1-Q22 with the specification's validation parameters, Q15 as a CTE, from the
# workload project.py --experiments runs.
TPCH = {
    path.stem: path.read_text(encoding="utf-8").strip()
    for path in sorted((Path(__file__).resolve().parents[1] / "queries").glob("Q*.sql"))
}

# Shapes that each broke forcing at some point, keyed by what they exercise.
SHAPES = {
    "10-table JOIN chain": (
        "SELECT count(*) FROM lineitem l JOIN orders o ON l.l_orderkey = o.o_orderkey "
        "JOIN customer c ON o.o_custkey = c.c_custkey "
        "JOIN nation cn ON c.c_nationkey = cn.n_nationkey "
        "JOIN region cr ON cn.n_regionkey = cr.r_regionkey "
        "JOIN supplier s ON l.l_suppkey = s.s_suppkey "
        "JOIN nation sn ON s.s_nationkey = sn.n_nationkey "
        "JOIN region sr ON sn.n_regionkey = sr.r_regionkey "
        "JOIN partsupp ps ON ps.ps_partkey = l.l_partkey AND ps.ps_suppkey = l.l_suppkey "
        "JOIN part p ON ps.ps_partkey = p.p_partkey "
        "WHERE cr.r_name <> sr.r_name AND p.p_brand = 'Brand#12' "
        "AND o.o_orderdate < DATE '1993-01-01'"
    ),
    "13-table FROM list (GEQO)": (
        "SELECT count(*) FROM lineitem l, orders o, customer c, nation cn, region cr, "
        "supplier s, nation sn, region sr, partsupp ps, part p, nation n3, region r3, "
        "customer c2 WHERE l.l_orderkey = o.o_orderkey AND o.o_custkey = c.c_custkey "
        "AND c.c_nationkey = cn.n_nationkey AND cn.n_regionkey = cr.r_regionkey "
        "AND l.l_suppkey = s.s_suppkey AND s.s_nationkey = sn.n_nationkey "
        "AND sn.n_regionkey = sr.r_regionkey AND ps.ps_partkey = l.l_partkey "
        "AND ps.ps_suppkey = l.l_suppkey AND ps.ps_partkey = p.p_partkey "
        "AND n3.n_nationkey = s.s_nationkey AND r3.r_regionkey = n3.n_regionkey "
        "AND c2.c_custkey = o.o_custkey AND p.p_size = 1"
    ),
    "flattened 9-table subquery joined to 3 tables": (
        "SELECT count(*) FROM (SELECT l.l_orderkey, c.c_nationkey FROM lineitem l "
        "JOIN orders o ON l.l_orderkey = o.o_orderkey JOIN customer c ON o.o_custkey = c.c_custkey "
        "JOIN nation cn ON c.c_nationkey = cn.n_nationkey "
        "JOIN region cr ON cn.n_regionkey = cr.r_regionkey "
        "JOIN supplier s ON l.l_suppkey = s.s_suppkey "
        "JOIN nation sn ON s.s_nationkey = sn.n_nationkey "
        "JOIN region sr ON sn.n_regionkey = sr.r_regionkey "
        "JOIN partsupp ps ON ps.ps_partkey = l.l_partkey AND ps.ps_suppkey = l.l_suppkey "
        "WHERE cr.r_name <> sr.r_name) big JOIN nation nn ON nn.n_nationkey = big.c_nationkey "
        "JOIN region rr ON rr.r_regionkey = nn.n_regionkey "
        "JOIN lineitem l2 ON l2.l_orderkey = big.l_orderkey AND l2.l_linenumber = 1"
    ),
    "LATERAL subquery joined": (
        "SELECT c.c_name, t.cnt FROM customer c, LATERAL (SELECT count(*) cnt FROM orders o "
        "WHERE o.o_custkey = c.c_custkey) t WHERE c.c_custkey < 100 AND t.cnt > 10"
    ),
    "rewrite next to a function alias": (
        "SELECT count(*) FROM nation, unnest(ARRAY[1, 2]) AS nation2(k) WHERE n_nationkey IN "
        "(SELECT n_nationkey FROM nation WHERE n_regionkey = nation2.k)"
    ),
    "rewrite of schema-qualified columns": (
        "SELECT count(*) FROM public.nation WHERE n_nationkey IN (SELECT public.nation.n_nationkey "
        "FROM public.nation WHERE public.nation.n_regionkey = 1)"
    ),
    "UNION subquery joined": (
        "SELECT count(*) FROM (SELECT s_nationkey AS k FROM supplier UNION "
        "SELECT c_nationkey FROM customer) u JOIN nation n ON n.n_nationkey = u.k "
        "JOIN region r ON r.r_regionkey = n.n_regionkey"
    ),
    "GROUP BY subquery joined": (
        "SELECT p.p_name, s.total FROM part p JOIN (SELECT l_partkey, sum(l_quantity) AS total "
        "FROM lineitem GROUP BY l_partkey) s ON s.l_partkey = p.p_partkey WHERE p.p_size = 15"
    ),
    "alias like a generated name": (
        "SELECT count(*) FROM customer c_1 JOIN customer c ON c.c_nationkey = c_1.c_nationkey "
        "WHERE c_1.c_custkey < 10 AND c.c_custkey < 10"
    ),
    "quoted mixed-case alias": (
        'SELECT count(*) FROM customer "Cust" JOIN orders o ON o.o_custkey = "Cust".c_custkey'
    ),
    "alias longer than 63 bytes": (
        "SELECT count(*) FROM customer a_very_long_alias_name_that_goes_on_and_on_and_on_for_"
        "sixty_three_bytes JOIN orders o ON o.o_custkey = a_very_long_alias_name_that_goes_on_"
        "and_on_and_on_for_sixty_three_bytes.c_custkey"
    ),
    "table reused in a correlated subquery": (
        "SELECT c_name FROM customer WHERE c_acctbal > "
        "(SELECT avg(c_acctbal) FROM customer WHERE c_nationkey = 3)"
    ),
    "UNION ALL reading one table twice": (
        "SELECT count(*) FROM (SELECT l_orderkey FROM lineitem WHERE l_quantity < 2 "
        "UNION ALL SELECT l_orderkey FROM lineitem WHERE l_quantity > 49) x "
        "JOIN orders ON orders.o_orderkey = x.l_orderkey"
    ),
    "recursive CTE": (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 10) "
        "SELECT sum(n) FROM r"
    ),
    "FULL JOIN": "SELECT count(*) FROM customer c FULL JOIN orders o ON o.o_custkey = c.c_custkey",
    "join of two outer-join results": (
        "SELECT count(*) FROM (nation n "
        "LEFT JOIN supplier s ON s.s_nationkey = n.n_nationkey) "
        "JOIN (region r LEFT JOIN customer c ON c.c_nationkey = r.r_regionkey) "
        "ON n.n_regionkey = r.r_regionkey"
    ),
    "joins below a FULL JOIN": (
        "SELECT l.l_returnflag, count(*) FROM region r "
        "JOIN nation n ON r.r_regionkey = n.n_regionkey "
        "JOIN supplier s ON n.n_nationkey = s.s_nationkey FULL JOIN lineitem l "
        "ON s.s_suppkey = l.l_suppkey GROUP BY l.l_returnflag"
    ),
    "nested FULL JOINs": (
        "SELECT count(*) FROM (supplier s FULL JOIN customer c "
        "ON s.s_nationkey = c.c_nationkey) FULL JOIN (nation n JOIN region r "
        "ON r.r_regionkey = n.n_regionkey) ON n.n_nationkey = s.s_nationkey"
    ),
    "IN subquery joined": (
        "SELECT count(*) FROM customer c WHERE c_custkey IN "
        "(SELECT o_custkey FROM orders o WHERE o_totalprice > 400000)"
    ),
    "grouped IN subquery joined": (
        "SELECT count(*) FROM customer c WHERE c_custkey IN "
        "(SELECT o_custkey FROM orders o GROUP BY o_custkey HAVING count(*) > 20)"
    ),
    "EXISTS inside EXISTS": (
        "SELECT count(*) FROM supplier s WHERE EXISTS (SELECT 1 FROM partsupp ps "
        "WHERE ps.ps_suppkey = s.s_suppkey AND EXISTS (SELECT 1 FROM lineitem l "
        "WHERE l.l_partkey = ps.ps_partkey AND l.l_suppkey = ps.ps_suppkey "
        "AND l.l_quantity > 49))"
    ),
    "CTE read once joined": (
        "WITH t AS (SELECT o_custkey, count(*) cnt FROM orders o GROUP BY o_custkey) "
        "SELECT c.c_name, t.cnt FROM customer c JOIN t ON t.o_custkey = c.c_custkey "
        "WHERE c.c_acctbal > 9000"
    ),
    "subquery with EXISTS joined": (
        "SELECT count(*) FROM nation n JOIN (SELECT c.c_nationkey k, count(*) cnt "
        "FROM customer c WHERE EXISTS (SELECT 1 FROM orders o "
        "WHERE o.o_custkey = c.c_custkey AND o.o_totalprice > 400000) "
        "GROUP BY c.c_nationkey) a ON a.k = n.n_nationkey"
    ),
    "OFFSET 0 subquery joined": (
        "SELECT count(*) FROM (SELECT l_orderkey FROM lineitem WHERE l_quantity > 45 "
        "OFFSET 0) t JOIN orders o ON o.o_orderkey = t.l_orderkey"
    ),
    "subquery without tables joined": (
        "SELECT count(*) FROM nation n JOIN (SELECT 1 AS k UNION ALL SELECT 2) u "
        "ON u.k = n.n_regionkey"
    ),
    "DISTINCT VALUES subquery joined": (
        "SELECT count(*) FROM supplier s JOIN nation n ON n.n_nationkey = s.s_nationkey "
        "JOIN (SELECT DISTINCT x FROM (VALUES (1), (2), (2)) v(x)) u ON u.x = n.n_regionkey"
    ),
    "two IN (VALUES) lists": (
        "SELECT count(*) FROM customer c WHERE c.c_nationkey IN (VALUES (1), (2), (3)) "
        "AND c.c_custkey IN (VALUES (10), (20), (30), (40))"
    ),
    "min and max of one column": "SELECT min(l_orderkey), max(l_orderkey) FROM lineitem",
    "8 tables and EXISTS over 2": (
        "SELECT count(*) FROM lineitem l JOIN orders o ON o.o_orderkey = l.l_orderkey "
        "JOIN customer c ON c.c_custkey = o.o_custkey "
        "JOIN nation cn ON cn.n_nationkey = c.c_nationkey "
        "JOIN region cr ON cr.r_regionkey = cn.n_regionkey "
        "JOIN supplier s ON s.s_suppkey = l.l_suppkey "
        "JOIN nation sn ON sn.n_nationkey = s.s_nationkey "
        "JOIN part p ON p.p_partkey = l.l_partkey WHERE o.o_orderkey < 8000 "
        "AND EXISTS (SELECT 1 FROM partsupp ps JOIN part p2 ON p2.p_partkey = ps.ps_partkey "
        "WHERE ps.ps_suppkey = s.s_suppkey AND p2.p_size = 1)"
    ),
    "join over UNION ALL of constants above 8 tables": (
        "SELECT count(*) FROM lineitem l JOIN orders o ON o.o_orderkey = l.l_orderkey "
        "JOIN customer c ON c.c_custkey = o.o_custkey "
        "JOIN nation cn ON cn.n_nationkey = c.c_nationkey "
        "JOIN region cr ON cr.r_regionkey = cn.n_regionkey "
        "JOIN supplier s ON s.s_suppkey = l.l_suppkey "
        "JOIN nation sn ON sn.n_nationkey = s.s_nationkey "
        "JOIN part p ON p.p_partkey = l.l_partkey "
        "JOIN (SELECT 1 AS k UNION ALL SELECT 2) u ON u.k = cr.r_regionkey "
        "WHERE o.o_orderkey < 8000"
    ),
    "INTERSECT": "SELECT o_custkey FROM orders INTERSECT SELECT c_custkey FROM customer",
    "TID scan": "SELECT * FROM region WHERE ctid = '(0,1)'",
    "hash-only GROUP BY": "SELECT xmin, count(*) FROM region GROUP BY xmin",
}

# PostgreSQL removes five of these LEFT JOINs, but splits the join by the ten it reads.
REMOVED_JOINS = (
    "SELECT count(*) FROM customer c LEFT JOIN orders o ON o.o_custkey = c.c_custkey "
    "LEFT JOIN lineitem l ON l.l_orderkey = o.o_orderkey "
    "LEFT JOIN part p ON p.p_partkey = l.l_partkey "
    "LEFT JOIN supplier s ON s.s_suppkey = l.l_suppkey "
    "LEFT JOIN nation sn ON sn.n_nationkey = s.s_nationkey "
    "LEFT JOIN region sr ON sr.r_regionkey = sn.n_regionkey "
    "JOIN nation cn ON cn.n_nationkey = c.c_nationkey "
    "JOIN region cr ON cr.r_regionkey = cn.n_regionkey "
    "LEFT JOIN partsupp ps ON ps.ps_partkey = l.l_partkey AND ps.ps_suppkey = l.l_suppkey "
    "WHERE c.c_custkey < 300"
)

# PostgreSQL copies the tables of a NOT MATERIALIZED CTE used twice, so no alias
# can tell the copies apart and forcing is refused.
REFUSED = (
    "WITH s AS NOT MATERIALIZED (SELECT s_suppkey, s_nationkey FROM supplier) "
    "SELECT count(*) FROM s a JOIN s b ON a.s_nationkey = b.s_nationkey"
)

-- Cross-border profit by customer region, supplier nation, and part brand.
-- Exercises nine joined relations, selective filters, grouping, and a top-N sort.
SELECT
    cr.r_name AS customer_region,
    sn.n_name AS supplier_nation,
    p.p_brand,
    COUNT(DISTINCT o.o_orderkey) AS order_count,
    SUM(l.l_extendedprice * (1 - l.l_discount)) AS revenue,
    SUM(
        l.l_extendedprice * (1 - l.l_discount)
        - ps.ps_supplycost * l.l_quantity
    ) AS estimated_profit
FROM customer AS c
JOIN nation AS cn ON cn.n_nationkey = c.c_nationkey
JOIN region AS cr ON cr.r_regionkey = cn.n_regionkey
JOIN orders AS o ON o.o_custkey = c.c_custkey
JOIN lineitem AS l ON l.l_orderkey = o.o_orderkey
JOIN part AS p ON p.p_partkey = l.l_partkey
JOIN partsupp AS ps
    ON ps.ps_partkey = l.l_partkey
    AND ps.ps_suppkey = l.l_suppkey
JOIN supplier AS s ON s.s_suppkey = ps.ps_suppkey
JOIN nation AS sn ON sn.n_nationkey = s.s_nationkey
WHERE o.o_orderdate >= DATE '1995-01-01'
  AND o.o_orderdate < DATE '1997-01-01'
  AND cr.r_name IN ('EUROPE', 'ASIA')
  AND cn.n_nationkey <> sn.n_nationkey
  AND p.p_size BETWEEN 5 AND 35
GROUP BY cr.r_name, sn.n_name, p.p_brand
HAVING SUM(l.l_extendedprice * (1 - l.l_discount)) > 100000
ORDER BY estimated_profit DESC, customer_region, supplier_nation, p.p_brand
LIMIT 50;

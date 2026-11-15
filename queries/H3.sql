-- Large multi-supplier orders without returned lines, ranked by net revenue.
-- Exercises aggregation before joins, NOT EXISTS, and a top-N sort.
SELECT
    c.c_custkey,
    c.c_name,
    o.o_orderkey,
    o.o_orderdate,
    totals.total_quantity,
    totals.supplier_count,
    totals.net_revenue
FROM (
    SELECT
        l.l_orderkey,
        SUM(l.l_quantity) AS total_quantity,
        COUNT(DISTINCT l.l_suppkey) AS supplier_count,
        SUM(l.l_extendedprice * (1 - l.l_discount)) AS net_revenue
    FROM lineitem AS l
    GROUP BY l.l_orderkey
    HAVING SUM(l.l_quantity) > 200
       AND COUNT(DISTINCT l.l_suppkey) >= 3
) AS totals
JOIN orders AS o ON o.o_orderkey = totals.l_orderkey
JOIN customer AS c ON c.c_custkey = o.o_custkey
WHERE o.o_orderdate >= DATE '1994-01-01'
  AND o.o_orderdate < DATE '1997-01-01'
  AND NOT EXISTS (
      SELECT 1
      FROM lineitem AS returned
      WHERE returned.l_orderkey = o.o_orderkey
        AND returned.l_returnflag = 'R'
  )
ORDER BY totals.net_revenue DESC, o.o_orderdate, o.o_orderkey
LIMIT 100;

-- Orders exceeding their customer's historical average, with a late receipt.
-- Exercises a correlated scalar aggregate, EXISTS, and customer-level aggregation.
SELECT
    c.c_custkey,
    c.c_name,
    n.n_name,
    COUNT(*) AS expensive_late_orders,
    SUM(o.o_totalprice) AS total_order_value
FROM customer AS c
JOIN nation AS n ON n.n_nationkey = c.c_nationkey
JOIN orders AS o ON o.o_custkey = c.c_custkey
WHERE c.c_mktsegment = 'BUILDING'
  AND o.o_orderdate >= DATE '1995-01-01'
  AND o.o_orderdate < DATE '1996-01-01'
  AND o.o_totalprice > (
      SELECT AVG(history.o_totalprice)
      FROM orders AS history
      WHERE history.o_custkey = c.c_custkey
        AND history.o_orderdate < DATE '1995-01-01'
  )
  AND EXISTS (
      SELECT 1
      FROM lineitem AS late
      WHERE late.l_orderkey = o.o_orderkey
        AND late.l_receiptdate > late.l_commitdate
  )
GROUP BY c.c_custkey, c.c_name, n.n_name
HAVING COUNT(*) >= 2
ORDER BY total_order_value DESC, c.c_custkey
LIMIT 50;

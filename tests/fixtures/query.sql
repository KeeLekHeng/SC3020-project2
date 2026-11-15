SELECT
    c.c_name,
    COUNT(*) AS order_count
FROM customer AS c
JOIN orders AS o
    ON c.c_custkey = o.o_custkey
WHERE o.o_orderdate >= DATE '1995-01-01'
GROUP BY c.c_custkey, c.c_name;

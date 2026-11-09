"""Queries the forcing tests run: TPC-H Q1-Q22 and shapes that broke forcing before."""

# TPC-H Q1-Q22 with the specification's validation parameters, Q15 as a CTE.
TPCH = {
"Q1": """select l_returnflag, l_linestatus, sum(l_quantity) as sum_qty, sum(l_extendedprice) as sum_base_price, sum(l_extendedprice * (1 - l_discount)) as sum_disc_price, sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) as sum_charge, avg(l_quantity) as avg_qty, avg(l_extendedprice) as avg_price, avg(l_discount) as avg_disc, count(*) as count_order from lineitem where l_shipdate <= date '1998-12-01' - interval '90 day' group by l_returnflag, l_linestatus order by l_returnflag, l_linestatus""",
"Q2": """select s_acctbal, s_name, n_name, p_partkey, p_mfgr, s_address, s_phone, s_comment from part, supplier, partsupp, nation, region where p_partkey = ps_partkey and s_suppkey = ps_suppkey and p_size = 15 and p_type like '%BRASS' and s_nationkey = n_nationkey and n_regionkey = r_regionkey and r_name = 'EUROPE' and ps_supplycost = (select min(ps_supplycost) from partsupp, supplier, nation, region where p_partkey = ps_partkey and s_suppkey = ps_suppkey and s_nationkey = n_nationkey and n_regionkey = r_regionkey and r_name = 'EUROPE') order by s_acctbal desc, n_name, s_name, p_partkey limit 100""",
"Q3": """select l_orderkey, sum(l_extendedprice * (1 - l_discount)) as revenue, o_orderdate, o_shippriority from customer, orders, lineitem where c_mktsegment = 'BUILDING' and c_custkey = o_custkey and l_orderkey = o_orderkey and o_orderdate < date '1995-03-15' and l_shipdate > date '1995-03-15' group by l_orderkey, o_orderdate, o_shippriority order by revenue desc, o_orderdate limit 10""",
"Q4": """select o_orderpriority, count(*) as order_count from orders where o_orderdate >= date '1993-07-01' and o_orderdate < date '1993-07-01' + interval '3 month' and exists (select * from lineitem where l_orderkey = o_orderkey and l_commitdate < l_receiptdate) group by o_orderpriority order by o_orderpriority""",
"Q5": """select n_name, sum(l_extendedprice * (1 - l_discount)) as revenue from customer, orders, lineitem, supplier, nation, region where c_custkey = o_custkey and l_orderkey = o_orderkey and l_suppkey = s_suppkey and c_nationkey = s_nationkey and s_nationkey = n_nationkey and n_regionkey = r_regionkey and r_name = 'ASIA' and o_orderdate >= date '1994-01-01' and o_orderdate < date '1994-01-01' + interval '1 year' group by n_name order by revenue desc""",
"Q6": """select sum(l_extendedprice * l_discount) as revenue from lineitem where l_shipdate >= date '1994-01-01' and l_shipdate < date '1994-01-01' + interval '1 year' and l_discount between 0.06 - 0.01 and 0.06 + 0.01 and l_quantity < 24""",
"Q7": """select supp_nation, cust_nation, l_year, sum(volume) as revenue from (select n1.n_name as supp_nation, n2.n_name as cust_nation, extract(year from l_shipdate) as l_year, l_extendedprice * (1 - l_discount) as volume from supplier, lineitem, orders, customer, nation n1, nation n2 where s_suppkey = l_suppkey and o_orderkey = l_orderkey and c_custkey = o_custkey and s_nationkey = n1.n_nationkey and c_nationkey = n2.n_nationkey and ((n1.n_name = 'FRANCE' and n2.n_name = 'GERMANY') or (n1.n_name = 'GERMANY' and n2.n_name = 'FRANCE')) and l_shipdate between date '1995-01-01' and date '1996-12-31') as shipping group by supp_nation, cust_nation, l_year order by supp_nation, cust_nation, l_year""",
"Q8": """select o_year, sum(case when nation = 'BRAZIL' then volume else 0 end) / sum(volume) as mkt_share from (select extract(year from o_orderdate) as o_year, l_extendedprice * (1 - l_discount) as volume, n2.n_name as nation from part, supplier, lineitem, orders, customer, nation n1, nation n2, region where p_partkey = l_partkey and s_suppkey = l_suppkey and l_orderkey = o_orderkey and o_custkey = c_custkey and c_nationkey = n1.n_nationkey and n1.n_regionkey = r_regionkey and r_name = 'AMERICA' and s_nationkey = n2.n_nationkey and o_orderdate between date '1995-01-01' and date '1996-12-31' and p_type = 'ECONOMY ANODIZED STEEL') as all_nations group by o_year order by o_year""",
"Q9": """select nation, o_year, sum(amount) as sum_profit from (select n_name as nation, extract(year from o_orderdate) as o_year, l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount from part, supplier, lineitem, partsupp, orders, nation where s_suppkey = l_suppkey and ps_suppkey = l_suppkey and ps_partkey = l_partkey and p_partkey = l_partkey and o_orderkey = l_orderkey and s_nationkey = n_nationkey and p_name like '%green%') as profit group by nation, o_year order by nation, o_year desc""",
"Q10": """select c_custkey, c_name, sum(l_extendedprice * (1 - l_discount)) as revenue, c_acctbal, n_name, c_address, c_phone, c_comment from customer, orders, lineitem, nation where c_custkey = o_custkey and l_orderkey = o_orderkey and o_orderdate >= date '1993-10-01' and o_orderdate < date '1993-10-01' + interval '3 month' and l_returnflag = 'R' and c_nationkey = n_nationkey group by c_custkey, c_name, c_acctbal, c_phone, n_name, c_address, c_comment order by revenue desc limit 20""",
"Q11": """select ps_partkey, sum(ps_supplycost * ps_availqty) as value from partsupp, supplier, nation where ps_suppkey = s_suppkey and s_nationkey = n_nationkey and n_name = 'GERMANY' group by ps_partkey having sum(ps_supplycost * ps_availqty) > (select sum(ps_supplycost * ps_availqty) * 0.001 from partsupp, supplier, nation where ps_suppkey = s_suppkey and s_nationkey = n_nationkey and n_name = 'GERMANY') order by value desc""",
"Q12": """select l_shipmode, sum(case when o_orderpriority = '1-URGENT' or o_orderpriority = '2-HIGH' then 1 else 0 end) as high_line_count, sum(case when o_orderpriority <> '1-URGENT' and o_orderpriority <> '2-HIGH' then 1 else 0 end) as low_line_count from orders, lineitem where o_orderkey = l_orderkey and l_shipmode in ('MAIL', 'SHIP') and l_commitdate < l_receiptdate and l_shipdate < l_commitdate and l_receiptdate >= date '1994-01-01' and l_receiptdate < date '1994-01-01' + interval '1 year' group by l_shipmode order by l_shipmode""",
"Q13": """select c_count, count(*) as custdist from (select c_custkey, count(o_orderkey) from customer left outer join orders on c_custkey = o_custkey and o_comment not like '%special%requests%' group by c_custkey) as c_orders (c_custkey, c_count) group by c_count order by custdist desc, c_count desc""",
"Q14": """select 100.00 * sum(case when p_type like 'PROMO%' then l_extendedprice * (1 - l_discount) else 0 end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue from lineitem, part where l_partkey = p_partkey and l_shipdate >= date '1995-09-01' and l_shipdate < date '1995-09-01' + interval '1 month'""",
"Q15": """with revenue0 (supplier_no, total_revenue) as (select l_suppkey, sum(l_extendedprice * (1 - l_discount)) from lineitem where l_shipdate >= date '1996-01-01' and l_shipdate < date '1996-01-01' + interval '3 month' group by l_suppkey) select s_suppkey, s_name, s_address, s_phone, total_revenue from supplier, revenue0 where s_suppkey = supplier_no and total_revenue = (select max(total_revenue) from revenue0) order by s_suppkey""",
"Q16": """select p_brand, p_type, p_size, count(distinct ps_suppkey) as supplier_cnt from partsupp, part where p_partkey = ps_partkey and p_brand <> 'Brand#45' and p_type not like 'MEDIUM POLISHED%' and p_size in (49, 14, 23, 45, 19, 3, 36, 9) and ps_suppkey not in (select s_suppkey from supplier where s_comment like '%Customer%Complaints%') group by p_brand, p_type, p_size order by supplier_cnt desc, p_brand, p_type, p_size""",
"Q17": """select sum(l_extendedprice) / 7.0 as avg_yearly from lineitem, part where p_partkey = l_partkey and p_brand = 'Brand#23' and p_container = 'MED BOX' and l_quantity < (select 0.2 * avg(l_quantity) from lineitem where l_partkey = p_partkey)""",
"Q18": """select c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice, sum(l_quantity) from customer, orders, lineitem where o_orderkey in (select l_orderkey from lineitem group by l_orderkey having sum(l_quantity) > 300) and c_custkey = o_custkey and o_orderkey = l_orderkey group by c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice order by o_totalprice desc, o_orderdate limit 100""",
"Q19": """select sum(l_extendedprice* (1 - l_discount)) as revenue from lineitem, part where (p_partkey = l_partkey and p_brand = 'Brand#12' and p_container in ('SM CASE', 'SM BOX', 'SM PACK', 'SM PKG') and l_quantity >= 1 and l_quantity <= 1 + 10 and p_size between 1 and 5 and l_shipmode in ('AIR', 'AIR REG') and l_shipinstruct = 'DELIVER IN PERSON') or (p_partkey = l_partkey and p_brand = 'Brand#23' and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK') and l_quantity >= 10 and l_quantity <= 10 + 10 and p_size between 1 and 10 and l_shipmode in ('AIR', 'AIR REG') and l_shipinstruct = 'DELIVER IN PERSON') or (p_partkey = l_partkey and p_brand = 'Brand#34' and p_container in ('LG CASE', 'LG BOX', 'LG PACK', 'LG PKG') and l_quantity >= 20 and l_quantity <= 20 + 10 and p_size between 1 and 15 and l_shipmode in ('AIR', 'AIR REG') and l_shipinstruct = 'DELIVER IN PERSON')""",
"Q20": """select s_name, s_address from supplier, nation where s_suppkey in (select ps_suppkey from partsupp where ps_partkey in (select p_partkey from part where p_name like 'forest%') and ps_availqty > (select 0.5 * sum(l_quantity) from lineitem where l_partkey = ps_partkey and l_suppkey = ps_suppkey and l_shipdate >= date '1994-01-01' and l_shipdate < date '1994-01-01' + interval '1 year')) and s_nationkey = n_nationkey and n_name = 'CANADA' order by s_name""",
"Q21": """select s_name, count(*) as numwait from supplier, lineitem l1, orders, nation where s_suppkey = l1.l_suppkey and o_orderkey = l1.l_orderkey and o_orderstatus = 'F' and l1.l_receiptdate > l1.l_commitdate and exists (select * from lineitem l2 where l2.l_orderkey = l1.l_orderkey and l2.l_suppkey <> l1.l_suppkey) and not exists (select * from lineitem l3 where l3.l_orderkey = l1.l_orderkey and l3.l_suppkey <> l1.l_suppkey and l3.l_receiptdate > l3.l_commitdate) and s_nationkey = n_nationkey and n_name = 'SAUDI ARABIA' group by s_name order by numwait desc, s_name limit 100""",
"Q22": """select cntrycode, count(*) as numcust, sum(c_acctbal) as totacctbal from (select substring(c_phone from 1 for 2) as cntrycode, c_acctbal from customer where substring(c_phone from 1 for 2) in ('13', '31', '23', '29', '30', '18', '17') and c_acctbal > (select avg(c_acctbal) from customer where c_acctbal > 0.00 and substring(c_phone from 1 for 2) in ('13', '31', '23', '29', '30', '18', '17')) and not exists (select * from orders where o_custkey = c_custkey)) as custsale group by cntrycode order by cntrycode""",
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
    "INTERSECT": "SELECT o_custkey FROM orders INTERSECT SELECT c_custkey FROM customer",
    "TID scan": "SELECT * FROM region WHERE ctid = '(0,1)'",
    "hash-only GROUP BY": "SELECT xmin, count(*) FROM region GROUP BY xmin",
}

# PostgreSQL copies the tables of a NOT MATERIALIZED CTE used twice, so no alias
# can tell the copies apart and forcing is refused.
REFUSED = (
    "WITH s AS NOT MATERIALIZED (SELECT s_suppkey, s_nationkey FROM supplier) "
    "SELECT count(*) FROM s a JOIN s b ON a.s_nationkey = b.s_nationkey"
)

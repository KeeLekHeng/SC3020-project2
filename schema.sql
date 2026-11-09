-- Sccsid:     @(#)dss.ddl	2.1.8.1
CREATE TABLE NATION  ( N_NATIONKEY  INTEGER NOT NULL,
                            N_NAME       CHAR(25) NOT NULL,
                            N_REGIONKEY  INTEGER NOT NULL,
                            N_COMMENT    VARCHAR(152));

CREATE TABLE REGION  ( R_REGIONKEY  INTEGER NOT NULL,
                            R_NAME       CHAR(25) NOT NULL,
                            R_COMMENT    VARCHAR(152));

CREATE TABLE PART  ( P_PARTKEY     INTEGER NOT NULL,
                          P_NAME        VARCHAR(55) NOT NULL,
                          P_MFGR        CHAR(25) NOT NULL,
                          P_BRAND       CHAR(10) NOT NULL,
                          P_TYPE        VARCHAR(25) NOT NULL,
                          P_SIZE        INTEGER NOT NULL,
                          P_CONTAINER   CHAR(10) NOT NULL,
                          P_RETAILPRICE DECIMAL(15,2) NOT NULL,
                          P_COMMENT     VARCHAR(23) NOT NULL );

CREATE TABLE SUPPLIER ( S_SUPPKEY     INTEGER NOT NULL,
                             S_NAME        CHAR(25) NOT NULL,
                             S_ADDRESS     VARCHAR(40) NOT NULL,
                             S_NATIONKEY   INTEGER NOT NULL,
                             S_PHONE       CHAR(15) NOT NULL,
                             S_ACCTBAL     DECIMAL(15,2) NOT NULL,
                             S_COMMENT     VARCHAR(101) NOT NULL);

CREATE TABLE PARTSUPP ( PS_PARTKEY     INTEGER NOT NULL,
                             PS_SUPPKEY     INTEGER NOT NULL,
                             PS_AVAILQTY    INTEGER NOT NULL,
                             PS_SUPPLYCOST  DECIMAL(15,2)  NOT NULL,
                             PS_COMMENT     VARCHAR(199) NOT NULL );

CREATE TABLE CUSTOMER ( C_CUSTKEY     INTEGER NOT NULL,
                             C_NAME        VARCHAR(25) NOT NULL,
                             C_ADDRESS     VARCHAR(40) NOT NULL,
                             C_NATIONKEY   INTEGER NOT NULL,
                             C_PHONE       CHAR(15) NOT NULL,
                             C_ACCTBAL     DECIMAL(15,2)   NOT NULL,
                             C_MKTSEGMENT  CHAR(10) NOT NULL,
                             C_COMMENT     VARCHAR(117) NOT NULL);

CREATE TABLE ORDERS  ( O_ORDERKEY       INTEGER NOT NULL,
                           O_CUSTKEY        INTEGER NOT NULL,
                           O_ORDERSTATUS    CHAR(1) NOT NULL,
                           O_TOTALPRICE     DECIMAL(15,2) NOT NULL,
                           O_ORDERDATE      DATE NOT NULL,
                           O_ORDERPRIORITY  CHAR(15) NOT NULL,
                           O_CLERK          CHAR(15) NOT NULL,
                           O_SHIPPRIORITY   INTEGER NOT NULL,
                           O_COMMENT        VARCHAR(79) NOT NULL);

CREATE TABLE LINEITEM ( L_ORDERKEY    INTEGER NOT NULL,
                             L_PARTKEY     INTEGER NOT NULL,
                             L_SUPPKEY     INTEGER NOT NULL,
                             L_LINENUMBER  INTEGER NOT NULL,
                             L_QUANTITY    DECIMAL(15,2) NOT NULL,
                             L_EXTENDEDPRICE  DECIMAL(15,2) NOT NULL,
                             L_DISCOUNT    DECIMAL(15,2) NOT NULL,
                             L_TAX         DECIMAL(15,2) NOT NULL,
                             L_RETURNFLAG  CHAR(1) NOT NULL,
                             L_LINESTATUS  CHAR(1) NOT NULL,
                             L_SHIPDATE    DATE NOT NULL,
                             L_COMMITDATE  DATE NOT NULL,
                             L_RECEIPTDATE DATE NOT NULL,
                             L_SHIPINSTRUCT CHAR(25) NOT NULL,
                             L_SHIPMODE     CHAR(10) NOT NULL,
                             L_COMMENT      VARCHAR(44) NOT NULL);

-- Primary keys defined by the project schema. PostgreSQL automatically
-- creates a unique B-tree index for each primary key.
ALTER TABLE REGION   ADD PRIMARY KEY (R_REGIONKEY);
ALTER TABLE NATION   ADD PRIMARY KEY (N_NATIONKEY);
ALTER TABLE PART     ADD PRIMARY KEY (P_PARTKEY);
ALTER TABLE SUPPLIER ADD PRIMARY KEY (S_SUPPKEY);
ALTER TABLE PARTSUPP ADD PRIMARY KEY (PS_PARTKEY, PS_SUPPKEY);
ALTER TABLE CUSTOMER ADD PRIMARY KEY (C_CUSTKEY);
ALTER TABLE ORDERS   ADD PRIMARY KEY (O_ORDERKEY);
ALTER TABLE LINEITEM ADD PRIMARY KEY
    (L_ORDERKEY, L_PARTKEY, L_SUPPKEY, L_LINENUMBER);

-- Foreign keys from the project brief. PostgreSQL uses them when it estimates
-- join sizes. Import the tables in the order listed in the README.
ALTER TABLE NATION   ADD CONSTRAINT FK_NATION
    FOREIGN KEY (N_REGIONKEY) REFERENCES REGION (R_REGIONKEY);
ALTER TABLE SUPPLIER ADD CONSTRAINT FK_SUPPLIER
    FOREIGN KEY (S_NATIONKEY) REFERENCES NATION (N_NATIONKEY);
ALTER TABLE PARTSUPP ADD CONSTRAINT FK_PS_SUPPKEY_PARTKEY
    FOREIGN KEY (PS_PARTKEY) REFERENCES PART (P_PARTKEY);
ALTER TABLE PARTSUPP ADD CONSTRAINT FK_PS_SUPPKEY_SUPPKEY
    FOREIGN KEY (PS_SUPPKEY) REFERENCES SUPPLIER (S_SUPPKEY);
ALTER TABLE CUSTOMER ADD CONSTRAINT FK_CUSTOMER
    FOREIGN KEY (C_NATIONKEY) REFERENCES NATION (N_NATIONKEY);
ALTER TABLE ORDERS   ADD CONSTRAINT FK_ORDERS
    FOREIGN KEY (O_CUSTKEY) REFERENCES CUSTOMER (C_CUSTKEY);
ALTER TABLE LINEITEM ADD CONSTRAINT FK_LINEITEM_ORDERKEY
    FOREIGN KEY (L_ORDERKEY) REFERENCES ORDERS (O_ORDERKEY);
ALTER TABLE LINEITEM ADD CONSTRAINT FK_LINEITEM_PARTKEY
    FOREIGN KEY (L_PARTKEY) REFERENCES PART (P_PARTKEY);
ALTER TABLE LINEITEM ADD CONSTRAINT FK_LINEITEM_SUPPKEY
    FOREIGN KEY (L_SUPPKEY) REFERENCES SUPPLIER (S_SUPPKEY);

-- Refresh planner statistics after the data is imported. Run ANALYZE again
-- after loading data when this schema is executed before the import.
ANALYZE;

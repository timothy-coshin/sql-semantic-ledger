-- Toy shop: SQL usage history of the analytics team (fictional).
-- LEDGER reads it with induce.load_history (statements separated by ';', ids q1, q2, ...).

-- q1: paid revenue, test orders excluded
SELECT SUM(o.amount) AS revenue
FROM orders AS o
WHERE o.status = 'paid' AND o.is_test = 0;

-- q2: paid revenue by region
SELECT c.region, SUM(o.amount) AS revenue
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.status = 'paid' AND o.is_test = 0
GROUP BY c.region
ORDER BY revenue DESC;

-- q3: monthly paid revenue
SELECT strftime('%Y-%m', created_at) AS month, SUM(amount) AS revenue
FROM orders
WHERE is_test = 0 AND status = 'paid'
GROUP BY month
ORDER BY month;

-- q4: paid revenue in the third quarter (a one-off date range)
SELECT SUM(amount)
FROM orders
WHERE status = 'paid' AND is_test = 0
  AND created_at >= '2026-07-01' AND created_at < '2026-10-01';

-- q5: number of paid orders
SELECT COUNT(*) FROM orders WHERE status = 'paid' AND is_test = 0;

-- q6: paid orders per month
SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS paid_orders
FROM orders
WHERE status = 'paid' AND is_test = 0
GROUP BY month;

-- q7: active customers (customers with at least one paid order)
SELECT COUNT(DISTINCT customer_id) AS active_customers
FROM orders
WHERE status = 'paid' AND is_test = 0;

-- q8: active customers by region
SELECT c.region, COUNT(DISTINCT o.customer_id) AS active_customers
FROM customers c
JOIN orders o ON o.customer_id = c.id
WHERE o.status = 'paid' AND o.is_test = 0
GROUP BY c.region;

-- q9: average order value
SELECT AVG(amount) AS average_order_value
FROM orders
WHERE status = 'paid' AND is_test = 0;

-- q10: average order value by month
SELECT strftime('%Y-%m', o.created_at) AS month, AVG(o.amount)
FROM orders o
WHERE o.is_test = 0 AND o.status = 'paid'
GROUP BY 1;

-- q11: refunded amount
SELECT SUM(amount) AS refunded
FROM orders
WHERE status = 'refunded' AND is_test = 0;

-- q12: refunds by region
SELECT c.region, SUM(o.amount) AS refunded
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.status = 'refunded' AND o.is_test = 0
GROUP BY c.region;

-- q13: top customers by paid revenue
SELECT c.name, SUM(o.amount) AS revenue
FROM customers c
JOIN orders o ON o.customer_id = c.id
WHERE o.status = 'paid' AND o.is_test = 0
GROUP BY c.name
ORDER BY revenue DESC
LIMIT 5;

-- q14: old dashboard tile, written before the status 'complete' was renamed to 'paid'
SELECT SUM(amount) FROM orders WHERE status = 'complete';

-- q15: old dashboard chart, same stale status
SELECT strftime('%Y-%m', created_at) AS month, SUM(amount)
FROM orders
WHERE status = 'complete'
GROUP BY month;

-- q16: customers who joined this year (a one-off question)
SELECT COUNT(*) FROM customers WHERE joined_at >= '2026-01-01';

-- q17: q1 run again later by another analyst (same query, so it is not independent support)
SELECT SUM(o.amount) AS revenue
FROM orders AS o
WHERE o.status = 'paid' AND o.is_test = 0;

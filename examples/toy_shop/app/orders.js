// Toy shop: orders API used by the web dashboard (fictional, Express-style handlers).
// The SQL in these template literals is evidence for LEDGER: `ledger scan-code examples/toy_shop/app`
// finds it, and `${...}` interpolations are read as parameters.
const express = require('express');
const db = require('./db');

const router = express.Router();

// Paid revenue since a date; test orders are excluded.
router.get('/api/revenue', async (req, res) => {
  const since = req.query.since || '2026-01-01';
  const row = await db.get(`
    SELECT SUM(o.amount) AS revenue
    FROM orders o
    WHERE o.status = 'paid'
      AND o.is_test = 0
      AND o.created_at >= ?
  `, [since]);
  res.json({ revenue: row.revenue });
});

// Paid revenue per region for the map widget.
router.get('/api/revenue/by-region', async (req, res) => {
  const rows = await db.all(`
    SELECT c.region, SUM(o.amount) AS revenue
    FROM orders o
    JOIN customers c ON c.id = o.customer_id
    WHERE o.status = 'paid' AND o.is_test = 0
    GROUP BY c.region
    ORDER BY c.region`);
  res.json(rows);
});

// Top customers; the limit is a number between 1 and 50.
router.get('/api/customers/top', async (req, res) => {
  const limit = Math.min(Math.max(Number.parseInt(req.query.limit, 10) || 5, 1), 50);
  const rows = await db.all(`
    SELECT c.name, SUM(o.amount) AS revenue
    FROM customers c
    JOIN orders o ON o.customer_id = c.id
    WHERE o.status = 'paid' AND o.is_test = 0
    GROUP BY c.name
    ORDER BY revenue DESC
    LIMIT ${limit}`);
  res.json(rows);
});

module.exports = router;

"""Toy shop: nightly KPI report for the finance team (fictional).

The SQL strings in this file are evidence for LEDGER: ``ledger scan-code examples/toy_shop/app`` finds them.

    python examples/toy_shop/app/report.py shop.sqlite [SINCE]
"""
import sqlite3
import sys

AVERAGE_ORDER_VALUE = """
    SELECT AVG(amount) AS average_order_value
    FROM orders
    WHERE status = 'paid' AND is_test = 0
"""

ACTIVE_CUSTOMERS = (
    "SELECT COUNT(DISTINCT customer_id) AS active_customers "
    "FROM orders "
    "WHERE status = 'paid' AND is_test = 0 AND created_at >= ?"
)


def kpis(conn, since):
    """Average order value (all time) and active customers since ``since`` (an ISO date)."""
    average = conn.execute(AVERAGE_ORDER_VALUE).fetchone()[0]
    active = conn.execute(ACTIVE_CUSTOMERS, (since,)).fetchone()[0]
    return {'average_order_value': average, 'active_customers': active}


if __name__ == '__main__':
    connection = sqlite3.connect(sys.argv[1])
    try:
        print(kpis(connection, sys.argv[2] if len(sys.argv) > 2 else '2026-01-01'))
    finally:
        connection.close()

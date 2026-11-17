"""Build the toy shop SQLite database used by the LEDGER examples.

    python examples/toy_shop/make_db.py [PATH]        (default: shop.sqlite in the current directory)

The data is fixed and fictional, so every run gives the same database:

* ``customers(id, name, region, joined_at)``: eight customers in three regions; customer 8 is the QA team's
  test account.
* ``orders(id, customer_id, status, amount, created_at, is_test)``: 36 orders from April to October 2026 with
  status 'paid', 'refunded', 'cancelled' or 'pending'. Test orders have ``is_test = 1``. No order has the old
  status 'complete', which a stale dashboard query in ``history.sql`` still uses.

:func:`add_shipped_orders` simulates a later data change: a new status 'shipped' appears.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

SCHEMA = """
CREATE TABLE customers (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    region    TEXT NOT NULL,
    joined_at TEXT NOT NULL
);
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    status      TEXT    NOT NULL,
    amount      REAL    NOT NULL,
    created_at  TEXT    NOT NULL,
    is_test     INTEGER NOT NULL DEFAULT 0
);
"""

CUSTOMERS: List[Tuple[int, str, str, str]] = [
    (1, 'Ana', 'Seoul', '2025-11-02'),
    (2, 'Bo', 'Seoul', '2025-12-15'),
    (3, 'Cy', 'Busan', '2026-01-20'),
    (4, 'Dee', 'Busan', '2026-02-03'),
    (5, 'Eli', 'Daegu', '2026-03-11'),
    (6, 'Fay', 'Daegu', '2026-04-25'),
    (7, 'Gus', 'Seoul', '2026-06-08'),
    (8, 'QA test account', 'Seoul', '2026-01-05'),
]

# (id, customer_id, status, amount, created_at, is_test)
ORDERS: List[Tuple[int, int, str, float, str, int]] = [
    (1, 1, 'paid', 42.0, '2026-04-03', 0),
    (2, 2, 'paid', 18.5, '2026-04-09', 0),
    (3, 3, 'paid', 65.0, '2026-04-17', 0),
    (4, 8, 'paid', 1.0, '2026-04-20', 1),
    (5, 4, 'cancelled', 30.0, '2026-04-26', 0),
    (6, 1, 'paid', 27.5, '2026-05-02', 0),
    (7, 5, 'paid', 54.0, '2026-05-11', 0),
    (8, 3, 'refunded', 22.0, '2026-05-15', 0),
    (9, 6, 'paid', 38.0, '2026-05-28', 0),
    (10, 8, 'paid', 1.0, '2026-05-30', 1),
    (11, 2, 'paid', 74.0, '2026-06-04', 0),
    (12, 4, 'paid', 19.0, '2026-06-12', 0),
    (13, 7, 'paid', 46.5, '2026-06-19', 0),
    (14, 5, 'pending', 33.0, '2026-06-27', 0),
    (15, 1, 'paid', 58.0, '2026-07-03', 0),
    (16, 6, 'refunded', 41.0, '2026-07-08', 0),
    (17, 3, 'paid', 23.5, '2026-07-14', 0),
    (18, 8, 'paid', 2.0, '2026-07-15', 1),
    (19, 7, 'paid', 88.0, '2026-07-22', 0),
    (20, 2, 'cancelled', 15.0, '2026-07-29', 0),
    (21, 4, 'paid', 61.0, '2026-08-05', 0),
    (22, 5, 'paid', 29.0, '2026-08-13', 0),
    (23, 1, 'refunded', 58.0, '2026-08-19', 0),
    (24, 6, 'paid', 36.5, '2026-08-26', 0),
    (25, 8, 'refunded', 2.0, '2026-08-27', 1),
    (26, 2, 'paid', 47.0, '2026-09-02', 0),
    (27, 3, 'paid', 52.0, '2026-09-10', 0),
    (28, 7, 'pending', 26.0, '2026-09-18', 0),
    (29, 5, 'paid', 71.5, '2026-09-24', 0),
    (30, 4, 'paid', 33.0, '2026-09-30', 0),
    (31, 1, 'paid', 44.0, '2026-10-06', 0),
    (32, 6, 'paid', 25.0, '2026-10-12', 0),
    (33, 8, 'paid', 3.0, '2026-10-14', 1),
    (34, 2, 'refunded', 74.0, '2026-10-20', 0),
    (35, 7, 'paid', 39.0, '2026-10-25', 0),
    (36, 3, 'cancelled', 64.0, '2026-10-30', 0),
]

# Orders with the new status 'shipped' (paid and handed to the carrier), added by add_shipped_orders.
SHIPPED_ORDERS: List[Tuple[int, int, str, float, str, int]] = [
    (37, 1, 'shipped', 49.0, '2026-11-03', 0),
    (38, 5, 'shipped', 35.5, '2026-11-05', 0),
    (39, 4, 'shipped', 62.0, '2026-11-08', 0),
]


def build_database(path: Union[str, Path], overwrite: bool = True) -> Path:
    """Create the toy shop database at ``path`` and return the path.

    An existing file is replaced when ``overwrite`` is true; otherwise FileExistsError is raised.
    """
    target = Path(path)
    if target.exists():
        if not overwrite:
            raise FileExistsError(f'{target} already exists')
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    try:
        conn.executescript(SCHEMA)
        conn.executemany('INSERT INTO customers VALUES (?, ?, ?, ?)', CUSTOMERS)
        conn.executemany('INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)', ORDERS)
        conn.commit()
    finally:
        conn.close()
    return target


def add_shipped_orders(path: Union[str, Path]) -> int:
    """Simulate a data change: insert orders with the new status 'shipped'. Returns the number inserted."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executemany('INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)', SHIPPED_ORDERS)
        conn.commit()
    finally:
        conn.close()
    return len(SHIPPED_ORDERS)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Build the toy shop SQLite database.')
    parser.add_argument('path', nargs='?', default='shop.sqlite', help='database file (replaced if it exists)')
    args = parser.parse_args(argv)
    path = build_database(args.path)
    print(f'wrote {path} ({len(CUSTOMERS)} customers, {len(ORDERS)} orders)')
    return 0


if __name__ == '__main__':
    sys.exit(main())

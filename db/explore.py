"""One-shot exploration script for the Olist dataset.

Run: python -m db.explore

Reads nothing, writes nothing — just executes a set of SELECTs and
prints their results so you can eyeball the actual data before writing
the Schema Understander prompt. The whole point is to surface gotchas
the build-plan template does not know about.

As you read the output, keep a scratch pad open and note anything
that surprises you. Especially:
  - Unexpected null rates
  - Distributions that look skewed in weird ways
  - Columns whose values do not match what the name implies
  - Date ranges (when did the data start / stop?)
  - Any value you would have to look up to interpret correctly
"""
from __future__ import annotations

import textwrap

import pandas as pd
from sqlalchemy import text

from db.connection import get_engine

pd.set_option("display.max_rows", 25)
pd.set_option("display.max_columns", 20)
pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 60)


# Each query: (heading, sql, why_we_care)
QUERIES: list[tuple[str, str, str]] = [
    (
        "Order status distribution",
        """
        SELECT order_status, COUNT(*) AS n
        FROM olist_orders_dataset
        GROUP BY order_status
        ORDER BY n DESC
        """,
        "How many canceled/unavailable orders exist? Any statuses you did not expect?",
    ),
    (
        "Date range of orders",
        """
        SELECT
            MIN(order_purchase_timestamp) AS first_order,
            MAX(order_purchase_timestamp) AS last_order,
            COUNT(*) FILTER (WHERE order_purchase_timestamp IS NULL) AS null_purchase_ts
        FROM olist_orders_dataset
        """,
        "When does the data start and stop? Any question that asks about dates outside this range is unanswerable.",
    ),
    (
        "Delivery timestamp coverage",
        """
        SELECT
            COUNT(*)                                                            AS total,
            COUNT(*) FILTER (WHERE order_delivered_customer_date IS NULL)       AS missing_delivered,
            COUNT(*) FILTER (WHERE order_approved_at IS NULL)                   AS missing_approved,
            COUNT(*) FILTER (WHERE order_delivered_carrier_date IS NULL)        AS missing_carrier
        FROM olist_orders_dataset
        """,
        "Which date columns are often missing? Delivery-performance questions must filter these.",
    ),
    (
        "Multi-item order — confirm order_item_id is a sequence",
        """
        SELECT order_id, order_item_id, product_id, price, freight_value
        FROM olist_order_items_dataset
        WHERE order_id = (
            SELECT order_id
            FROM olist_order_items_dataset
            GROUP BY order_id
            HAVING COUNT(*) >= 4
            LIMIT 1
        )
        ORDER BY order_item_id
        """,
        "Look at the order_item_id column — it should increment 1,2,3,4... NOT be a quantity.",
    ),
    (
        "Installment payments — confirm multi-row per order",
        """
        SELECT order_id, payment_sequential, payment_type,
               payment_installments, payment_value
        FROM olist_order_payments_dataset
        WHERE order_id IN (
            SELECT order_id
            FROM olist_order_payments_dataset
            GROUP BY order_id
            HAVING COUNT(*) > 1
            LIMIT 2
        )
        ORDER BY order_id, payment_sequential
        """,
        "Same order appears multiple times. Summing payment_value blindly would double-count.",
    ),
    (
        "Payment types",
        """
        SELECT payment_type, COUNT(*) AS n,
               ROUND(AVG(payment_value)::numeric, 2) AS avg_value
        FROM olist_order_payments_dataset
        GROUP BY payment_type
        ORDER BY n DESC
        """,
        "What payment types exist? Any that look weird (e.g. 'not_defined')?",
    ),
    (
        "Review score distribution (nulls included)",
        """
        SELECT review_score, COUNT(*) AS n
        FROM olist_order_reviews_dataset
        GROUP BY review_score
        ORDER BY review_score NULLS LAST
        """,
        "Are there null review_scores? How many?",
    ),
    (
        "Top 20 product categories (in Portuguese)",
        """
        SELECT product_category_name, COUNT(*) AS n
        FROM olist_products_dataset
        GROUP BY product_category_name
        ORDER BY n DESC
        LIMIT 20
        """,
        "These are Portuguese names. The translation table has English equivalents.",
    ),
    (
        "Categories WITHOUT English translation",
        """
        SELECT p.product_category_name, COUNT(*) AS n_products
        FROM olist_products_dataset p
        LEFT JOIN product_category_name_translation t
               ON p.product_category_name = t.product_category_name
        WHERE t.product_category_name_english IS NULL
        GROUP BY p.product_category_name
        ORDER BY n_products DESC
        """,
        "Which categories fall through an English translation? Analyst queries must handle these.",
    ),
    (
        "Revenue from order_items vs. revenue from payments (Q3 2017)",
        """
        WITH q3_orders AS (
            SELECT order_id
            FROM olist_orders_dataset
            WHERE order_purchase_timestamp >= DATE '2017-07-01'
              AND order_purchase_timestamp <  DATE '2017-10-01'
              AND order_status NOT IN ('canceled', 'unavailable')
        ),
        items_sum AS (
            SELECT SUM(price) AS items_revenue,
                   SUM(freight_value) AS items_freight
            FROM olist_order_items_dataset
            WHERE order_id IN (SELECT order_id FROM q3_orders)
        ),
        payments_sum AS (
            SELECT SUM(payment_value) AS payments_revenue
            FROM olist_order_payments_dataset
            WHERE order_id IN (SELECT order_id FROM q3_orders)
        )
        SELECT
            (SELECT items_revenue FROM items_sum)                 AS items_revenue_excl_freight,
            (SELECT items_freight FROM items_sum)                 AS items_freight,
            (SELECT items_revenue + items_freight FROM items_sum) AS items_revenue_incl_freight,
            (SELECT payments_revenue FROM payments_sum)           AS payments_revenue
        """,
        "The north-star example. payments_revenue should be close to items_revenue_incl_freight (small delta from installment rounding).",
    ),
    (
        "Review score null rate",
        """
        SELECT
            COUNT(*) AS total_reviews,
            COUNT(*) FILTER (WHERE review_score IS NULL) AS null_scores,
            ROUND(100.0 * COUNT(*) FILTER (WHERE review_score IS NULL) / COUNT(*), 2) AS pct_null
        FROM olist_order_reviews_dataset
        """,
        "If you ever AVG(review_score) you must filter nulls.",
    ),
    (
        "Customer vs customer_unique_id — are they the same?",
        """
        SELECT
            COUNT(DISTINCT customer_id)         AS distinct_customer_id,
            COUNT(DISTINCT customer_unique_id)  AS distinct_customer_unique_id,
            COUNT(*)                            AS total_rows
        FROM olist_customers_dataset
        """,
        "Olist has TWO customer IDs. If these numbers differ, one maps to an order and the other to a person.",
    ),
]


def run_one(engine, heading: str, sql: str, why: str) -> None:
    print("=" * 80)
    print(heading)
    print("-" * 80)
    print(textwrap.fill(f"WHY: {why}", width=80))
    print()
    df = pd.read_sql(text(sql), engine)
    print(df.to_string(index=False))
    print()


def main() -> None:
    engine = get_engine()
    for heading, sql, why in QUERIES:
        run_one(engine, heading, sql, why)
    print("=" * 80)
    print("Done. Re-read the WHY lines and jot down anything that surprised you.")


if __name__ == "__main__":
    main()

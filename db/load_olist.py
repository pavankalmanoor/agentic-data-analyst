"""Load the Olist Brazilian e-commerce dataset into Postgres.

Usage:
    python -m db.load_olist                # loads from ./data/
    python -m db.load_olist --data ./foo   # loads from ./foo/

Assumes the nine Olist CSVs (downloaded from Kaggle "brazilian-ecommerce"
by olistbr) are present in the --data directory. The geolocation table
is intentionally skipped — it is large and not needed for Phase 1.

The loader:
  1. Creates the eight tables with explicit Postgres types (dates as
     timestamps, IDs as varchar, money as numeric).
  2. Loads each CSV with COPY via psycopg2 for speed.
  3. Adds primary keys and foreign keys AFTER loading, so load order
     does not matter.
  4. Prints row counts at the end for verification.

Running this script is idempotent: it drops and recreates the eight
tables every time. Do not point it at a production database.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from db.connection import get_engine


# ---------------------------------------------------------------------------
# Schema — column types are explicit so Pandas does not guess wrong.
# ---------------------------------------------------------------------------
# Each entry: (table_name, csv_filename, CREATE TABLE body, primary_key_sql)
TABLES: list[tuple[str, str, str, str]] = [
    (
        "olist_customers_dataset",
        "olist_customers_dataset.csv",
        """
        customer_id               VARCHAR(64),
        customer_unique_id        VARCHAR(64),
        customer_zip_code_prefix  VARCHAR(16),
        customer_city             TEXT,
        customer_state            VARCHAR(4)
        """,
        "customer_id",
    ),
    (
        "olist_orders_dataset",
        "olist_orders_dataset.csv",
        """
        order_id                       VARCHAR(64),
        customer_id                    VARCHAR(64),
        order_status                   VARCHAR(32),
        order_purchase_timestamp       TIMESTAMP,
        order_approved_at              TIMESTAMP,
        order_delivered_carrier_date   TIMESTAMP,
        order_delivered_customer_date  TIMESTAMP,
        order_estimated_delivery_date  TIMESTAMP
        """,
        "order_id",
    ),
    (
        "olist_order_items_dataset",
        "olist_order_items_dataset.csv",
        """
        order_id             VARCHAR(64),
        order_item_id        INTEGER,
        product_id           VARCHAR(64),
        seller_id            VARCHAR(64),
        shipping_limit_date  TIMESTAMP,
        price                NUMERIC(12, 2),
        freight_value        NUMERIC(12, 2)
        """,
        "order_id, order_item_id",
    ),
    (
        "olist_order_payments_dataset",
        "olist_order_payments_dataset.csv",
        """
        order_id              VARCHAR(64),
        payment_sequential    INTEGER,
        payment_type          VARCHAR(32),
        payment_installments  INTEGER,
        payment_value         NUMERIC(12, 2)
        """,
        "order_id, payment_sequential",
    ),
    (
        "olist_order_reviews_dataset",
        "olist_order_reviews_dataset.csv",
        """
        review_id                VARCHAR(64),
        order_id                 VARCHAR(64),
        review_score             INTEGER,
        review_comment_title     TEXT,
        review_comment_message   TEXT,
        review_creation_date     TIMESTAMP,
        review_answer_timestamp  TIMESTAMP
        """,
        # review_id is NOT unique in the raw data (same reviewer can
        # review multiple orders); use composite key.
        "review_id, order_id",
    ),
    (
        "olist_products_dataset",
        "olist_products_dataset.csv",
        """
        product_id                  VARCHAR(64),
        product_category_name       VARCHAR(128),
        product_name_lenght         INTEGER,
        product_description_lenght  INTEGER,
        product_photos_qty          INTEGER,
        product_weight_g            INTEGER,
        product_length_cm           INTEGER,
        product_height_cm           INTEGER,
        product_width_cm            INTEGER
        """,
        "product_id",
    ),
    (
        "olist_sellers_dataset",
        "olist_sellers_dataset.csv",
        """
        seller_id               VARCHAR(64),
        seller_zip_code_prefix  VARCHAR(16),
        seller_city             TEXT,
        seller_state            VARCHAR(4)
        """,
        "seller_id",
    ),
    (
        "product_category_name_translation",
        "product_category_name_translation.csv",
        """
        product_category_name          VARCHAR(128),
        product_category_name_english  VARCHAR(128)
        """,
        "product_category_name",
    ),
]


# Foreign keys added AFTER load so order of inserts does not matter.
# Note: we deliberately do NOT add a FK from products.product_category_name
# to the translation table, because three categories exist in products
# with no translation row (pc_gamer, portateis_cozinha_e_preparadores_de_alimentos,
# and NULL). See Schema Understander gotcha #5.
FOREIGN_KEYS: list[tuple[str, str, str, str]] = [
    # (table, column, references_table, references_column)
    ("olist_orders_dataset", "customer_id",
     "olist_customers_dataset", "customer_id"),
    ("olist_order_items_dataset", "order_id",
     "olist_orders_dataset", "order_id"),
    ("olist_order_items_dataset", "product_id",
     "olist_products_dataset", "product_id"),
    ("olist_order_items_dataset", "seller_id",
     "olist_sellers_dataset", "seller_id"),
    ("olist_order_payments_dataset", "order_id",
     "olist_orders_dataset", "order_id"),
    ("olist_order_reviews_dataset", "order_id",
     "olist_orders_dataset", "order_id"),
]


def drop_and_create_tables(engine) -> None:
    """Drop all target tables (CASCADE) and recreate empty shells."""
    with engine.begin() as conn:
        # Drop in reverse so FKs unwind cleanly; CASCADE as belt-and-braces.
        for table_name, *_ in reversed(TABLES):
            conn.execute(text(f'DROP TABLE IF EXISTS {table_name} CASCADE'))
        for table_name, _csv, body, _pk in TABLES:
            conn.execute(text(f'CREATE TABLE {table_name} ({body.strip()})'))


def load_csv(engine, table_name: str, csv_path: Path) -> int:
    """Load one CSV into a table and return the row count loaded."""
    # pandas handles the quoting / embedded newlines in review comments
    # that would trip up a naive COPY. We pay a small speed cost for safety.
    df = pd.read_csv(csv_path)
    # Strip trailing whitespace on varchar-ish columns; Olist CSVs have some.
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).where(df[col].notna(), None)
    df.to_sql(
        table_name,
        engine,
        if_exists="append",
        index=False,
        method="multi",
        chunksize=5000,
    )
    return len(df)


def add_primary_keys(engine) -> None:
    with engine.begin() as conn:
        for table_name, _csv, _body, pk in TABLES:
            conn.execute(text(
                f'ALTER TABLE {table_name} ADD PRIMARY KEY ({pk})'
            ))


def add_foreign_keys(engine) -> None:
    with engine.begin() as conn:
        for table, col, ref_table, ref_col in FOREIGN_KEYS:
            fk_name = f"fk_{table}_{col}"
            conn.execute(text(
                f'ALTER TABLE {table} '
                f'ADD CONSTRAINT {fk_name} '
                f'FOREIGN KEY ({col}) REFERENCES {ref_table}({ref_col})'
            ))


def verify_row_counts(engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table_name, *_ in TABLES:
            result = conn.execute(text(f'SELECT COUNT(*) FROM {table_name}'))
            counts[table_name] = result.scalar_one()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data", default="data",
        help="Directory containing the Olist CSVs (default: ./data)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data).resolve()
    if not data_dir.is_dir():
        print(f"ERROR: --data directory not found: {data_dir}", file=sys.stderr)
        return 1

    # Verify every CSV is present before we touch the database.
    missing = [
        csv for _t, csv, *_ in TABLES
        if not (data_dir / csv).is_file()
    ]
    if missing:
        print("ERROR: missing CSVs in data directory:", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        return 1

    engine = get_engine()
    t0 = time.time()

    print("Dropping and recreating tables...")
    drop_and_create_tables(engine)

    print("Loading CSVs...")
    for table_name, csv, *_ in TABLES:
        n = load_csv(engine, table_name, data_dir / csv)
        print(f"  {table_name:<40s} {n:>7d} rows")

    print("Adding primary keys...")
    add_primary_keys(engine)

    print("Adding foreign keys...")
    add_foreign_keys(engine)

    print("\nRow counts:")
    for table_name, count in verify_row_counts(engine).items():
        print(f"  {table_name:<40s} {count:>7d}")

    print(f"\nDone in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Creates sample tables (users, products, orders, order_items) and fills them with fake data.
Safe to run again: it drops and recreates only these four tables."""
import random
import psycopg
from faker import Faker
from app.config import DATABASE_URL

N_USERS, N_PRODUCTS, N_ORDERS = 5000, 200, 20000

SCHEMA = """
DROP TABLE IF EXISTS order_items, orders, products, users CASCADE;

CREATE TABLE users (
    id          bigserial PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE products (
    id     bigserial PRIMARY KEY,
    name   text NOT NULL,
    price  numeric(10,2) NOT NULL
);
CREATE TABLE orders (
    id          bigserial PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id),
    status      text NOT NULL,
    total       numeric(10,2) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE order_items (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id),
    product_id  bigint NOT NULL REFERENCES products(id),
    qty         int NOT NULL
);
"""


def main() -> None:
    fake = Faker()
    Faker.seed(42)
    random.seed(42)
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)

        users = [(f"user{i}@{fake.domain_name()}", fake.name()) for i in range(1, N_USERS + 1)]
        cur.executemany("INSERT INTO users (email, name) VALUES (%s, %s)", users)

        products = [(fake.catch_phrase()[:40], round(random.uniform(3, 500), 2)) for _ in range(N_PRODUCTS)]
        cur.executemany("INSERT INTO products (name, price) VALUES (%s, %s)", products)

        statuses = ["pending", "paid", "shipped", "cancelled"]
        orders = [(random.randint(1, N_USERS), random.choice(statuses), round(random.uniform(5, 900), 2))
                  for _ in range(N_ORDERS)]
        cur.executemany("INSERT INTO orders (user_id, status, total) VALUES (%s, %s, %s)", orders)

        items = [(oid, random.randint(1, N_PRODUCTS), random.randint(1, 5))
                 for oid in range(1, N_ORDERS + 1) for _ in range(random.randint(1, 3))]
        cur.executemany("INSERT INTO order_items (order_id, product_id, qty) VALUES (%s, %s, %s)", items)

        cur.execute("ANALYZE")  # lets Postgres record row counts, which the schema reader shows

        for t in ("users", "products", "orders", "order_items"):
            cur.execute(f"SELECT count(*) FROM {t}")
            print(f"{t:12} {cur.fetchone()[0]} rows")


if __name__ == "__main__":
    main()

import json
import os
from config import DATA_FILE


def save_to_json(new_data):
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception:
                data = []
    else:
        data = []

    data.append(new_data)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def execute_neon_insert(sql_query):
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return False

    try:
        import psycopg

        conn = psycopg.connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(sql_query)
            conn.commit()
            return True
        finally:
            conn.close()
    except ImportError:
        try:
            import psycopg2
        except ImportError:
            return False

        conn = psycopg2.connect(database_url)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql_query)
            return True
        finally:
            conn.close()

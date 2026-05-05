import json
import os
from config import DATA_FILE, ADMIN_FILE, DEFAULT_ADMIN_ID


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


def get_admins():
    if os.path.exists(ADMIN_FILE):
        try:
            with open(ADMIN_FILE, "r", encoding="utf-8") as f:
                admins = json.load(f)
                return admins if isinstance(admins, list) else [DEFAULT_ADMIN_ID]
        except Exception:
            return [DEFAULT_ADMIN_ID]
    return [DEFAULT_ADMIN_ID]


def add_admin(user_id):
    admins = get_admins()
    if str(user_id) not in admins:
        admins.append(str(user_id))
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump(admins, f, indent=4, ensure_ascii=False)
        return True
    return False


def is_admin(user_id):
    return str(user_id) in get_admins()


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

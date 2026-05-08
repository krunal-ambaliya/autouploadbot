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
    user_id_str = str(user_id)
    if user_id_str not in admins:
        admins.append(user_id_str)
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump(admins, f, indent=4, ensure_ascii=False)
        return True
    return False


def remove_admin(user_id):
    user_id_str = str(user_id)
    if user_id_str == str(DEFAULT_ADMIN_ID):
        return False, "Cannot remove the master admin."
    
    admins = get_admins()
    if user_id_str in admins:
        admins.remove(user_id_str)
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump(admins, f, indent=4, ensure_ascii=False)
        return True, f"Admin {user_id_str} removed successfully."
    return False, f"User {user_id_str} is not an admin."


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

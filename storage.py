import json
import os
import logging
from config import DATA_FILE, ADMIN_FILE, DEFAULT_ADMIN_ID

logger = logging.getLogger(__name__)

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


def init_db():
    """Initialize database tables if they don't exist."""
    create_table_query = """
    CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    success = execute_neon_query(create_table_query)
    if success:
        logger.info("Database initialized successfully.")
        # Migrate existing admins if any
        migrate_admins()
    else:
        logger.error("Failed to initialize database.")


def migrate_admins():
    """Migrate admins from admins.json to database if database is empty."""
    if not os.path.exists(ADMIN_FILE):
        return

    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            admins = json.load(f)
            if not isinstance(admins, list):
                return
    except Exception:
        return

    # Check if DB has any admins beyond the default
    existing = get_admins()
    # If we only have the default admin (which is always in get_admins results), migrate
    if len(existing) <= 1: 
        for admin_id in admins:
            add_admin(admin_id)
        logger.info(f"Migrated {len(admins)} admins from {ADMIN_FILE} to database.")


def get_admins():
    query = "SELECT user_id FROM admins"
    rows = execute_neon_fetch(query)
    admins = []
    if rows:
        admins = [str(row[0]) for row in rows]
    
    # Always ensure default admin is in the list
    master_id = str(DEFAULT_ADMIN_ID)
    if master_id not in admins:
        admins.append(master_id)
    
    return admins


def add_admin(user_id):
    user_id_str = str(user_id)
    query = f"INSERT INTO admins (user_id) VALUES ('{user_id_str}') ON CONFLICT (user_id) DO NOTHING"
    return execute_neon_query(query)


def remove_admin(user_id):
    user_id_str = str(user_id)
    if user_id_str == str(DEFAULT_ADMIN_ID):
        return False, "Cannot remove the master admin."
    
    query = f"DELETE FROM admins WHERE user_id = '{user_id_str}'"
    success = execute_neon_query(query)
    if success:
        return True, f"Admin {user_id_str} removed successfully."
    return False, f"Failed to remove admin {user_id_str}."


def is_admin(user_id):
    return str(user_id) in get_admins()


def _get_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is not set.")
        return None

    try:
        import psycopg
        return psycopg.connect(database_url)
    except ImportError:
        try:
            import psycopg2
            return psycopg2.connect(database_url)
        except ImportError:
            logger.error("Neither psycopg nor psycopg2 found.")
            return None
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        return None


def execute_neon_query(sql_query):
    """Executes a query (INSERT, UPDATE, DELETE, CREATE)."""
    conn = _get_connection()
    if not conn:
        return False

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql_query)
        return True
    except Exception as e:
        logger.error(f"Database query error: {e}")
        return False
    finally:
        conn.close()


def execute_neon_fetch(sql_query):
    """Executes a SELECT query and returns rows."""
    conn = _get_connection()
    if not conn:
        return None

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql_query)
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Database fetch error: {e}")
        return None
    finally:
        conn.close()

# Alias for backward compatibility
execute_neon_insert = execute_neon_query

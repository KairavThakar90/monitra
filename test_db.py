import psycopg2
import sys

db_url = "postgresql://neondb_owner:npg_csd1PNJ7ROvg@ep-cool-frog-az05chmu.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"

try:
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'users';")
    columns = cur.fetchall()
    print("Columns in users table:")
    for col in columns:
        print(f"  {col[0]}: {col[1]}")
    
    cur.execute("SELECT id, capture_frequency, idle_enabled, idle_minutes FROM users LIMIT 1;")
    user = cur.fetchone()
    print("\nSample user:")
    print(user)
    
except Exception as e:
    print(f"Error: {e}")
finally:
    if 'conn' in locals() and conn:
        conn.close()

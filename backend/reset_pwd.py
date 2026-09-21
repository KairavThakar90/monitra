import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import sys
import os
import requests

sys.path.append(os.path.dirname(__file__))

from app.models.user import User
from app.core.security import hash_password

db_url = "postgresql://neondb_owner:npg_csd1PNJ7ROvg@ep-cool-frog-az05chmu.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
engine = create_engine(db_url)
Session = sessionmaker(bind=engine)
session = Session()

try:
    user = session.query(User).filter_by(role_name="administrator").first()
    if not user:
        print("No admin found.")
        sys.exit(1)
        
    print(f"Using admin: {user.email}")
    user.password_hash = hash_password("Password123!")
    session.commit()
    print("Password reset.")

except Exception as e:
    print(f"Error: {e}")
finally:
    session.close()

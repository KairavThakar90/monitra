import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import sys
import os
import requests
from jose import jwt

sys.path.append(os.path.dirname(__file__))

from app.models.user import User
from app.core.security import create_access_token

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
    token = create_access_token({"user_id": user.id})

    url = f"https://monitra-lvzq.vercel.app/api/v1/members/{user.id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "capture_frequency": 15,
        "idle_enabled": False,
        "idle_minutes": 10
    }
    response = requests.patch(url, headers=headers, json=payload)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
    
except Exception as e:
    print(f"Error: {e}")
finally:
    session.close()

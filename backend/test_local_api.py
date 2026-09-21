import requests
from app.core.security import create_access_token
import sys
import os
sys.path.append(os.path.dirname(__file__))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.user import User

db_url = "postgresql://neondb_owner:npg_csd1PNJ7ROvg@ep-cool-frog-az05chmu.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
engine = create_engine(db_url)
Session = sessionmaker(bind=engine)
session = Session()

user = session.query(User).filter_by(role_name="administrator").first()
token = create_access_token({"user_id": user.id})
session.close()

url = f"http://127.0.0.1:8000/api/v1/members/{user.id}"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}
payload = {
    "capture_frequency": 25,
    "idle_enabled": False,
    "idle_minutes": 15
}
r = requests.patch(url, headers=headers, json=payload)
print(f"Patch Status: {r.status_code}")
print(f"Patch Response: {r.text}")

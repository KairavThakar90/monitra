import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import sys
import os

sys.path.append(os.path.dirname(__file__))

from app.models.user import User

db_url = "postgresql://neondb_owner:npg_csd1PNJ7ROvg@ep-cool-frog-az05chmu.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
engine = create_engine(db_url)
Session = sessionmaker(bind=engine)
session = Session()

try:
    users = session.query(User).all()
    print("Users with dates:")
    for user in users[:5]:
        print(f"ID: {user.id}, DOJ: {user.date_of_joining}, DOB: {user.date_of_birth}")
except Exception as e:
    print(f"Error: {e}")
finally:
    session.close()

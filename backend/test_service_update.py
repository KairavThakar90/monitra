import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import sys
import os

sys.path.append(os.path.dirname(__file__))

from app.models.user import User
from app.schemas.member import MemberUpdate
from app.services.member_service import MemberService
from app.repositories.member import MemberRepository

db_url = "postgresql://neondb_owner:npg_csd1PNJ7ROvg@ep-cool-frog-az05chmu.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
engine = create_engine(db_url)
Session = sessionmaker(bind=engine)
session = Session()

try:
    # Get a real admin user
    current_user = session.query(User).filter_by(role_name="administrator").first()
    if not current_user:
        print("No admin found.")
        sys.exit(1)
        
    print(f"Using admin: {current_user.email}")

    # Try updating with MemberService
    payload = MemberUpdate(capture_frequency=15, idle_enabled=False, idle_minutes=10)
    updated = MemberService.update(session, current_user, current_user.id, payload)
    
    print("\nAfter update:")
    print("Capture Frequency:", updated.capture_frequency)
    print("Idle Enabled:", updated.idle_enabled)
    print("Idle Minutes:", updated.idle_minutes)
    
except Exception as e:
    print(f"Error: {e}")
finally:
    session.close()

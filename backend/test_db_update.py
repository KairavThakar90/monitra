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
    # Get any user
    user = session.query(User).first()
    print("Before update:")
    print("Capture Frequency:", user.capture_frequency)
    print("Idle Enabled:", user.idle_enabled)
    print("Idle Minutes:", user.idle_minutes)
    
    # Try updating
    data = {"capture_frequency": 20, "idle_enabled": False, "idle_minutes": 10}
    MemberRepository.save(session, user, data)
    
    # Reload
    session.refresh(user)
    print("\nAfter update:")
    print("Capture Frequency:", user.capture_frequency)
    print("Idle Enabled:", user.idle_enabled)
    print("Idle Minutes:", user.idle_minutes)
    
    # Restore
    data = {"capture_frequency": 10, "idle_enabled": True, "idle_minutes": 5}
    MemberRepository.save(session, user, data)
    
except Exception as e:
    print(f"Error: {e}")
finally:
    session.close()

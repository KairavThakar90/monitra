import sys
import os

sys.path.append(os.path.dirname(__file__))

from app.schemas.member import MemberResponse
from datetime import datetime

data = {
    "id": 1,
    "name": "Test",
    "email": "test@test.com",
    "role_name": "employee",
    "status": "active",
    "date_of_joining": None,
    "date_of_birth": None,
    "designation": "Dev",
    "idle_enabled": True,
    "idle_minutes": 5,
    "capture_frequency": 10,
    "created_at": datetime.utcnow(),
    "updated_at": datetime.utcnow()
}

resp = MemberResponse(**data)
print(resp.model_dump_json())

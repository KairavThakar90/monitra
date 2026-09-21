from app.schemas.member import MemberUpdate

payload = MemberUpdate(capture_frequency=10, idle_enabled=True, idle_minutes=5)
print(payload.model_dump(exclude_unset=True, mode="python"))

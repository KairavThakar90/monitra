from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.core.database import Base

class ScreenshotApplication(Base):
    __tablename__ = "screenshot_applications"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True, nullable=False)
    process_name = Column(String, index=True, nullable=False)
    category = Column(String, nullable=False)
    description = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

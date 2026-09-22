from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, CheckConstraint
from sqlalchemy.sql import func
from app.core.database import Base

class ScreenshotExclusion(Base):
    __tablename__ = "screenshot_exclusions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    application_id = Column(Integer, ForeignKey("screenshot_applications.id"), nullable=True, index=True)
    url_id = Column(Integer, ForeignKey("screenshot_urls.id"), nullable=True, index=True)
    exclusion_type = Column(String, nullable=False) # 'application' or 'url'
    is_excluded = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("exclusion_type IN ('application', 'url')", name="check_valid_exclusion_type"),
        CheckConstraint(
            "(exclusion_type = 'application' AND application_id IS NOT NULL AND url_id IS NULL) OR "
            "(exclusion_type = 'url' AND url_id IS NOT NULL AND application_id IS NULL)",
            name="check_exclusion_refs"
        ),
    )

from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class ScreenshotApplicationBase(BaseModel):
    name: str
    process_name: str
    category: str
    description: Optional[str] = None
    is_active: bool = True

class ScreenshotApplicationCreate(ScreenshotApplicationBase):
    pass

class ScreenshotApplicationUpdate(BaseModel):
    name: Optional[str] = None
    process_name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

class ScreenshotApplicationResponse(ScreenshotApplicationBase):
    id: int
    created_at: datetime
    updated_at: datetime
    class Config:
        orm_mode = True

class ScreenshotUrlBase(BaseModel):
    name: str
    domain: str
    url_pattern: str
    category: str
    is_active: bool = True

class ScreenshotUrlCreate(ScreenshotUrlBase):
    pass

class ScreenshotUrlUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    url_pattern: Optional[str] = None
    category: Optional[str] = None
    is_active: Optional[bool] = None

class ScreenshotUrlResponse(ScreenshotUrlBase):
    id: int
    created_at: datetime
    updated_at: datetime
    class Config:
        orm_mode = True

class ScreenshotExclusionBase(BaseModel):
    user_id: int
    application_id: Optional[int] = None
    url_id: Optional[int] = None
    exclusion_type: str
    is_excluded: bool = True

class ScreenshotExclusionCreate(ScreenshotExclusionBase):
    pass

class ScreenshotExclusionUpdate(BaseModel):
    is_excluded: bool

class ScreenshotExclusionResponse(ScreenshotExclusionBase):
    id: int
    created_at: datetime
    updated_at: datetime
    class Config:
        orm_mode = True

class PrivacyConfigResponse(BaseModel):
    applications: List[ScreenshotApplicationResponse]
    urls: List[ScreenshotUrlResponse]
    excluded_applications: List[ScreenshotExclusionResponse]
    excluded_urls: List[ScreenshotExclusionResponse]

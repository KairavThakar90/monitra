from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.screenshot_application import ScreenshotApplication
from app.models.screenshot_url import ScreenshotUrl
from app.models.screenshot_exclusion import ScreenshotExclusion
from app.schemas.screenshot_privacy import (
    ScreenshotApplicationCreate, ScreenshotApplicationUpdate, ScreenshotApplicationResponse,
    ScreenshotUrlCreate, ScreenshotUrlUpdate, ScreenshotUrlResponse,
    ScreenshotExclusionCreate, ScreenshotExclusionUpdate, ScreenshotExclusionResponse,
    PrivacyConfigResponse
)

router = APIRouter(prefix="/api/v1/screenshot", tags=["Screenshot Privacy"])

# --- Admin APIs ---

@router.get("/applications", response_model=List[ScreenshotApplicationResponse])
def list_applications(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # TODO: Add admin role check if needed
    return db.query(ScreenshotApplication).all()

@router.post("/applications", response_model=ScreenshotApplicationResponse)
def create_application(req: ScreenshotApplicationCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    app = ScreenshotApplication(**req.dict())
    db.add(app)
    db.commit()
    db.refresh(app)
    return app

@router.put("/applications/{app_id}", response_model=ScreenshotApplicationResponse)
def update_application(app_id: int, req: ScreenshotApplicationUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    app = db.query(ScreenshotApplication).filter(ScreenshotApplication.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    for k, v in req.dict(exclude_unset=True).items():
        setattr(app, k, v)
    db.commit()
    db.refresh(app)
    return app

@router.delete("/applications/{app_id}")
def delete_application(app_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    app = db.query(ScreenshotApplication).filter(ScreenshotApplication.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    db.delete(app)
    db.commit()
    return {"ok": True}

@router.get("/urls", response_model=List[ScreenshotUrlResponse])
def list_urls(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(ScreenshotUrl).all()

@router.post("/urls", response_model=ScreenshotUrlResponse)
def create_url(req: ScreenshotUrlCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    url = ScreenshotUrl(**req.dict())
    db.add(url)
    db.commit()
    db.refresh(url)
    return url

@router.put("/urls/{url_id}", response_model=ScreenshotUrlResponse)
def update_url(url_id: int, req: ScreenshotUrlUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    url = db.query(ScreenshotUrl).filter(ScreenshotUrl.id == url_id).first()
    if not url:
        raise HTTPException(status_code=404, detail="URL not found")
    for k, v in req.dict(exclude_unset=True).items():
        setattr(url, k, v)
    db.commit()
    db.refresh(url)
    return url

@router.delete("/urls/{url_id}")
def delete_url(url_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    url = db.query(ScreenshotUrl).filter(ScreenshotUrl.id == url_id).first()
    if not url:
        raise HTTPException(status_code=404, detail="URL not found")
    db.delete(url)
    db.commit()
    return {"ok": True}

# --- User exclusions ---

@router.get("/users/{user_id}/screenshot-exclusions", response_model=List[ScreenshotExclusionResponse])
def list_user_exclusions(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(ScreenshotExclusion).filter(ScreenshotExclusion.user_id == user_id).all()

@router.post("/users/{user_id}/screenshot-exclusions", response_model=ScreenshotExclusionResponse)
def create_exclusion(user_id: int, req: ScreenshotExclusionCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if req.user_id != user_id:
        raise HTTPException(status_code=400, detail="User ID mismatch")
    
    # Optional: check if one already exists
    query = db.query(ScreenshotExclusion).filter(
        ScreenshotExclusion.user_id == user_id,
        ScreenshotExclusion.exclusion_type == req.exclusion_type
    )
    if req.exclusion_type == "application":
        query = query.filter(ScreenshotExclusion.application_id == req.application_id)
    else:
        query = query.filter(ScreenshotExclusion.url_id == req.url_id)
        
    existing = query.first()
    if existing:
        existing.is_excluded = req.is_excluded
        db.commit()
        db.refresh(existing)
        return existing

    excl = ScreenshotExclusion(**req.dict())
    db.add(excl)
    db.commit()
    db.refresh(excl)
    return excl

@router.put("/users/{user_id}/screenshot-exclusions/{exclusion_id}", response_model=ScreenshotExclusionResponse)
def update_exclusion(user_id: int, exclusion_id: int, req: ScreenshotExclusionUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    excl = db.query(ScreenshotExclusion).filter(ScreenshotExclusion.id == exclusion_id, ScreenshotExclusion.user_id == user_id).first()
    if not excl:
        raise HTTPException(status_code=404, detail="Exclusion not found")
    excl.is_excluded = req.is_excluded
    db.commit()
    db.refresh(excl)
    return excl

@router.delete("/users/{user_id}/screenshot-exclusions/{exclusion_id}")
def delete_exclusion(user_id: int, exclusion_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    excl = db.query(ScreenshotExclusion).filter(ScreenshotExclusion.id == exclusion_id, ScreenshotExclusion.user_id == user_id).first()
    if not excl:
        raise HTTPException(status_code=404, detail="Exclusion not found")
    db.delete(excl)
    db.commit()
    return {"ok": True}

# --- Desktop Config ---

@router.get("/privacy-config", response_model=PrivacyConfigResponse)
def get_privacy_config(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # Load all active apps and urls
    apps = db.query(ScreenshotApplication).filter(ScreenshotApplication.is_active == True).all()
    urls = db.query(ScreenshotUrl).filter(ScreenshotUrl.is_active == True).all()
    
    # Load exclusions for current user
    exclusions = db.query(ScreenshotExclusion).filter(ScreenshotExclusion.user_id == current_user.id, ScreenshotExclusion.is_excluded == True).all()
    
    excluded_apps = [e for e in exclusions if e.exclusion_type == "application"]
    excluded_urls = [e for e in exclusions if e.exclusion_type == "url"]
    
    return {
        "applications": apps,
        "urls": urls,
        "excluded_applications": excluded_apps,
        "excluded_urls": excluded_urls,
    }

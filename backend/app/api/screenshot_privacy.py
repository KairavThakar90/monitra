from typing import List
from fastapi import APIRouter, Depends, HTTPException, status

from app.services.desktop_config_revision import DesktopConfigRevisionService
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.screenshot_application import ScreenshotApplication
from app.models.screenshot_url import ScreenshotUrl
from app.models.screenshot_exclusion import ScreenshotExclusion
from app.schemas.screenshot_privacy import (
    ScreenshotApplicationCreate, ScreenshotApplicationUpdate, ScreenshotApplicationResponse,
    ScreenshotApplicationCreatedResponse,
    ScreenshotUrlCreate, ScreenshotUrlUpdate, ScreenshotUrlResponse, ScreenshotUrlCreatedResponse,
    ScreenshotExclusionCreate, ScreenshotExclusionUpdate, ScreenshotExclusionResponse,
    PrivacyConfigResponse
)
from app.services.member_display import MemberDisplayService
from app.services.screenshot_privacy import ScreenshotPrivacyService

router = APIRouter(prefix="/api/v1/screenshot", tags=["Screenshot Privacy"])


def require_rule_manager(current_user: User = Depends(get_current_user)) -> User:
    """Administrators and HR: the only people who may change a privacy rule or anyone's exclusions. Enforced
    here on the server for every client -- the web page hiding a button is a courtesy, not the control."""
    return ScreenshotPrivacyService.require_rule_manager(current_user)

# --- Admin APIs ---

@router.get("/applications", response_model=List[ScreenshotApplicationResponse])
def list_applications(db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    # The catalogue itself is readable by any signed-in user (it is what the desktop is configured from); changing it is not.
    return db.query(ScreenshotApplication).all()

@router.post(
    "/applications",
    dependencies=[Depends(require_rule_manager)],
    response_model=ScreenshotApplicationCreatedResponse,
    summary="Add an application rule, optionally switching it on for all members or chosen members",
    description=(
        "With `apply_to` omitted this only adds the rule to the shared catalogue. With "
        "`apply_to` (`{\"scope\": \"all\"}` or `{\"scope\": \"members\", \"user_ids\": [...]}`) the rule "
        "is also switched on for those members in the same transaction -- **administrators only**. "
        "`all` is every active member of the caller's organization today; later joiners are not covered."
    ),
    responses={
        400: {"description": "A listed member does not exist in the caller's organization. Nothing was created."},
        403: {"description": "`apply_to` was sent by someone who is not an administrator. Nothing was created."},
    },
)
def create_application(req: ScreenshotApplicationCreate, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    # Every refusal happens here, before the first write.
    member_ids = ScreenshotPrivacyService.members_for(db, current_user, req.apply_to)
    app = ScreenshotApplication(**req.dict(exclude={"apply_to"}))
    db.add(app)
    db.flush()
    applied = ScreenshotPrivacyService.exclude_members(db, member_ids, application_id=app.id)
    db.commit()
    db.refresh(app)
    DesktopConfigRevisionService.bump(db, member_id=None, what="privacy_application_created")
    return ScreenshotApplicationCreatedResponse.model_validate(app, from_attributes=True).model_copy(update={"applied_to_count": applied})

@router.put("/applications/{app_id}", response_model=ScreenshotApplicationResponse, dependencies=[Depends(require_rule_manager)])
def update_application(app_id: int, req: ScreenshotApplicationUpdate, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    app = db.query(ScreenshotApplication).filter(ScreenshotApplication.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    for k, v in req.dict(exclude_unset=True).items():
        setattr(app, k, v)
    db.commit()
    db.refresh(app)
    DesktopConfigRevisionService.bump(db, member_id=None, what="privacy_application_updated")
    return app

@router.delete("/applications/{app_id}", dependencies=[Depends(require_rule_manager)])
def delete_application(app_id: int, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    app = db.query(ScreenshotApplication).filter(ScreenshotApplication.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    db.delete(app)
    db.commit()
    DesktopConfigRevisionService.bump(db, member_id=None, what="privacy_application_deleted")
    return {"ok": True}

@router.get("/urls", response_model=List[ScreenshotUrlResponse])
def list_urls(db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    return db.query(ScreenshotUrl).all()

@router.post(
    "/urls",
    dependencies=[Depends(require_rule_manager)],
    response_model=ScreenshotUrlCreatedResponse,
    summary="Add a website rule, optionally switching it on for all members or chosen members",
    description="As `POST /applications`: `apply_to` is optional and, when present, administrators only.",
    responses={
        400: {"description": "A listed member does not exist in the caller's organization. Nothing was created."},
        403: {"description": "`apply_to` was sent by someone who is not an administrator. Nothing was created."},
    },
)
def create_url(req: ScreenshotUrlCreate, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    member_ids = ScreenshotPrivacyService.members_for(db, current_user, req.apply_to)
    url = ScreenshotUrl(**req.dict(exclude={"apply_to"}))
    db.add(url)
    db.flush()
    applied = ScreenshotPrivacyService.exclude_members(db, member_ids, url_id=url.id)
    db.commit()
    db.refresh(url)
    DesktopConfigRevisionService.bump(db, member_id=None, what="privacy_url_created")
    return ScreenshotUrlCreatedResponse.model_validate(url, from_attributes=True).model_copy(update={"applied_to_count": applied})

@router.put("/urls/{url_id}", response_model=ScreenshotUrlResponse, dependencies=[Depends(require_rule_manager)])
def update_url(url_id: int, req: ScreenshotUrlUpdate, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    url = db.query(ScreenshotUrl).filter(ScreenshotUrl.id == url_id).first()
    if not url:
        raise HTTPException(status_code=404, detail="URL not found")
    for k, v in req.dict(exclude_unset=True).items():
        setattr(url, k, v)
    db.commit()
    db.refresh(url)
    DesktopConfigRevisionService.bump(db, member_id=None, what="privacy_url_updated")
    return url

@router.delete("/urls/{url_id}", dependencies=[Depends(require_rule_manager)])
def delete_url(url_id: int, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    url = db.query(ScreenshotUrl).filter(ScreenshotUrl.id == url_id).first()
    if not url:
        raise HTTPException(status_code=404, detail="URL not found")
    db.delete(url)
    db.commit()
    DesktopConfigRevisionService.bump(db, member_id=None, what="privacy_url_deleted")
    return {"ok": True}

# --- User exclusions ---

@router.get("/users/{user_id}/screenshot-exclusions", response_model=List[ScreenshotExclusionResponse])
def list_user_exclusions(user_id: int, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    # Whose exclusions these are is that person's business and their administrators': anyone else is refused.
    if user_id != current_user.id:
        ScreenshotPrivacyService.require_rule_manager(current_user)
        ScreenshotPrivacyService.member_in_caller_organization(db, current_user, user_id)
    return db.query(ScreenshotExclusion).filter(ScreenshotExclusion.user_id == user_id).all()

@router.post("/users/{user_id}/screenshot-exclusions", response_model=ScreenshotExclusionResponse, dependencies=[Depends(require_rule_manager)])
def create_exclusion(user_id: int, req: ScreenshotExclusionCreate, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    if req.user_id != user_id:
        raise HTTPException(status_code=400, detail="User ID mismatch")
    ScreenshotPrivacyService.member_in_caller_organization(db, current_user, user_id)
    
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
        DesktopConfigRevisionService.bump(db, member_id=user_id, what="privacy_exclusion_updated")
        return existing

    excl = ScreenshotExclusion(**req.dict())
    db.add(excl)
    db.commit()
    db.refresh(excl)
    DesktopConfigRevisionService.bump(db, member_id=user_id, what="privacy_exclusion_created")
    return excl

@router.put("/users/{user_id}/screenshot-exclusions/{exclusion_id}", response_model=ScreenshotExclusionResponse, dependencies=[Depends(require_rule_manager)])
def update_exclusion(user_id: int, exclusion_id: int, req: ScreenshotExclusionUpdate, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    ScreenshotPrivacyService.member_in_caller_organization(db, current_user, user_id)
    excl = db.query(ScreenshotExclusion).filter(ScreenshotExclusion.id == exclusion_id, ScreenshotExclusion.user_id == user_id).first()
    if not excl:
        raise HTTPException(status_code=404, detail="Exclusion not found")
    excl.is_excluded = req.is_excluded
    db.commit()
    db.refresh(excl)
    DesktopConfigRevisionService.bump(db, member_id=user_id, what="privacy_exclusion_updated")
    return excl

@router.delete("/users/{user_id}/screenshot-exclusions/{exclusion_id}", dependencies=[Depends(require_rule_manager)])
def delete_exclusion(user_id: int, exclusion_id: int, db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
    ScreenshotPrivacyService.member_in_caller_organization(db, current_user, user_id)
    excl = db.query(ScreenshotExclusion).filter(ScreenshotExclusion.id == exclusion_id, ScreenshotExclusion.user_id == user_id).first()
    if not excl:
        raise HTTPException(status_code=404, detail="Exclusion not found")
    db.delete(excl)
    db.commit()
    DesktopConfigRevisionService.bump(db, member_id=user_id, what="privacy_exclusion_deleted")
    return {"ok": True}

# --- Desktop Config ---

@router.get("/privacy-config", response_model=PrivacyConfigResponse)
def get_privacy_config(db: Session = Depends(get_db, scope="function"), current_user: User = Depends(get_current_user)):
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
        # Screens an administrator switched capture off for (Screenshot Management
        # -> Screens). Here because this is the member-specific "what is not
        # captured" answer the desktop has to hold before its first capture.
        "excluded_displays": MemberDisplayService.excluded_numbers(db, current_user),
    }

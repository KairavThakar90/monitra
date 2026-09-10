import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.database import get_db
from app.repositories.user import UserRepository
from app.models.user import User
from passlib.context import CryptContext

security_scheme = HTTPBearer(auto_error=False)

def create_access_token(claims: Dict[str, Any], expires_delta: timedelta = None) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = claims.copy()
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def generate_refresh_token() -> str:
    return secrets.token_hex(32)

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme),
    db: Session = Depends(get_db)
) -> User:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    
    token = credentials.credentials

    # Two kinds of bearer credential arrive here, and they are told apart by a
    # prefix a JWT can never have. A service credential is a long-lived API key
    # belonging to a machine (today: the release pipeline); everything else is
    # an access token belonging to a person.
    #
    # The import is deliberately local: app.services.service_credential imports
    # `hash_token` from this module, so importing it at module scope would be a
    # cycle.
    from app.services.service_credential import (
        ServiceCredentialService, looks_like_service_key,
    )

    if looks_like_service_key(token):
        return ServiceCredentialService.authenticate(db, token)

    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    
    user = UserRepository.get_by_id(db, user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    return user

def forbid_service_principal(current_user: User = Depends(get_current_user)) -> User:
    """Authenticate, but refuse a machine.

    For the handful of endpoints whose whole purpose is to hand a *person* a
    session — the desktop-to-web handoff mints a link that opens the web client
    signed in. A CI key must not be able to turn itself into a browser session:
    that would convert a credential scoped to one job into an interactive
    login, which is precisely the escalation a service credential exists to
    avoid.
    """
    if getattr(current_user, "is_service_principal", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A service credential cannot open an interactive session.",
        )
    return current_user


def require_permission(permission_name: str):
    def dependency(current_user: User = Depends(get_current_user)) -> User:
        permissions = current_user.permissions or {}
        if not permissions.get(permission_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action"
            )
        return current_user
    return dependency


import bcrypt

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    try:
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    except Exception:
        return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        if not hashed_password:
            return False
        hashed = hashed_password
        if hashed.startswith("$2y$"):
            hashed = "$2b$" + hashed[4:]
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        try:
            return pwd_context.verify(plain_password, hashed_password)
        except Exception:
            return False
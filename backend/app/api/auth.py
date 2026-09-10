from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.schemas.user import UserRead, DevLoginRequest, LoginRequest, SsoTokenRequest
from app.schemas.token import LogoutRequest, RefreshRequest, SsoHandoffResponse, TokenPair
from app.services.auth import AuthService
from app.models.user import User
from app.core.security import get_current_user
from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post(
    "/login",
    response_model=TokenPair,
    summary="Sign in with Hubstaff credentials",
    description=(
        "Desktop and Swagger clients use this endpoint. The backend securely "
        "forwards the supplied credentials to the configured Hubstaff provider "
        "login URL, provisions the local user when needed, and returns an SMS JWT. "
        "`login_for` names the client the session is for (`Desktop` or `Web`) and "
        "is passed on to the provider; it defaults to `Desktop`."
    ),
    responses={
        401: {"description": "Provider rejected the supplied credentials"},
        502: {"description": "Provider rejected or blocked the backend request"},
        503: {"description": "Provider is unavailable"},
        504: {"description": "Provider timed out"},
    },
)
async def login(payload: LoginRequest, db: Session = Depends(get_db)):
    try:
        return await AuthService.login_exchange(
            db, payload.username, payload.password, payload.login_for
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Authentication exchange failed: {str(e)}")

@router.post(
    "/sso/token",
    response_model=TokenPair,
    summary="Exchange a provider single sign-on token for a local session",
    description=(
        "The performance portal sends users here with its own JWT in the URL "
        "(`?token=...`). The backend verifies that token with the provider, reads "
        "the identity behind it, and returns the local token pair so the web client "
        "can go straight to the dashboard. The provider token is never accepted as a "
        "local credential."
    ),
    responses={
        401: {"description": "Token is invalid, expired, or was rejected by the provider"},
        403: {"description": "No active local account exists for the authenticated identity"},
        502: {"description": "Provider returned an unusable response"},
        503: {"description": "Provider is unavailable"},
    },
)
async def sso_token_login(payload: SsoTokenRequest, db: Session = Depends(get_db)):
    try:
        return await AuthService.sso_exchange(db, payload.token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Single sign-on exchange failed: {str(e)}")

@router.post(
    "/sso/handoff",
    response_model=SsoHandoffResponse,
    summary="Mint a single-use token that opens the web client already signed in",
    description=(
        "The desktop client calls this with its own access token when the user "
        "opens Profile, then launches the web client as `/?token=...`. The web "
        "client exchanges it at `/auth/sso/token` for a real session. The token "
        "is valid for seconds and can be redeemed only once; if it is missed or "
        "replayed the browser simply shows the login screen."
    ),
    responses={401: {"description": "The desktop session is not valid"}},
)
def sso_handoff(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    token, expires_at = AuthService.issue_handoff_token(db, current_user)
    return SsoHandoffResponse(token=token, expires_at=expires_at)


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Renew an access token without re-entering credentials",
    description=(
        "Exchanges a valid refresh token for a fresh access token and a rotated "
        "refresh token. The presented token is single-use. Refreshing never "
        "extends the sign-in window: `session_expires_at` is always measured "
        "from the original login, so a client that refreshes indefinitely still "
        "has to sign in again when that window closes."
    ),
    responses={
        401: {"description": "Session is expired, revoked, unknown, or its user is inactive"},
    },
)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    return AuthService.refresh_session(db, payload.refresh_token)


@router.post(
    "/logout",
    status_code=204,
    summary="Revoke the current session",
    description=(
        "Ends the session the refresh token belongs to. Idempotent: an unknown "
        "or already-revoked token succeeds, so a client clearing local state "
        "never gets stuck retrying."
    ),
)
def logout(payload: LogoutRequest, db: Session = Depends(get_db)):
    AuthService.revoke_session(db, payload.refresh_token)
    return Response(status_code=204)


@router.get("/me", response_model=UserRead)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.post("/dev-login", response_model=TokenPair)
def dev_login(payload: DevLoginRequest, db: Session = Depends(get_db)):
    if settings.ENV == "production":
        raise HTTPException(status_code=404)
    return AuthService.dev_login(db, payload.email, payload.password)

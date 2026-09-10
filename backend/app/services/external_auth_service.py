import logging
import ssl
import uuid
import certifi
import httpx
import urllib3.util.ssl_
from fastapi import HTTPException, status
from app.core.config import settings
from app.schemas.user import DEFAULT_LOGIN_FOR, LoginFor

logger = logging.getLogger("uvicorn.error")

#: What the backend advertises it can accept from the provider.
#:
#: Deliberately *without* `br`. httpx can only decode Brotli when the optional
#: `brotli`/`brotlicffi` package is installed, and it is not a dependency of
#: this service. The live provider honours `br` the moment it is offered, so
#: advertising it meant a perfectly good HTTP 200 login response came back as
#: undecodable bytes: `response.json()` raised, and the user was told
#: "Authentication service is temporarily unavailable" for correct credentials.
#: Never add an encoding here that httpx cannot actually decode.
ACCEPT_ENCODING = "gzip, deflate"

class ExternalAuthService:
    @staticmethod
    def _create_ssl_context() -> ssl.SSLContext:
        """Create a custom SSL context using urllib3 configuration to avoid JA3 blocks."""
        context = urllib3.util.ssl_.create_urllib3_context()
        context.load_verify_locations(cafile=certifi.where())
        return context

    @classmethod
    async def authenticate(
        cls,
        username: str,
        password: str,
        login_for: LoginFor = DEFAULT_LOGIN_FOR,
    ) -> dict:
        """
        Authenticate credentials against the external Pantheon service.
        Returns the user data dict on success.
        Raises HTTPException for controlled outcomes.

        `login_for` tells the provider which client the sign-in is for. It is
        constrained to the set `LoginFor` names, so nothing a caller invents
        reaches the provider.
        """
        normalized_username = username.strip()
        logger.info(
            "AUTH_LOGIN_STARTED: Initiating login exchange for user: %s (login_for=%s)",
            normalized_username,
            login_for,
        )
        
        url = settings.WORDPRESS_LOGIN_URL
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": settings.WORDPRESS_LOGIN_USER_AGENT,
            "Cache-Control": "no-cache",
            "Postman-Token": str(uuid.uuid4()),
            "Accept-Encoding": ACCEPT_ENCODING,
        }
        payload = {"username": normalized_username, "password": password, "login_for": login_for}
        
        ssl_ctx = cls._create_ssl_context()
        
        logger.info("AUTH_PROVIDER_REQUEST_STARTED: Outbound request to provider url: %s", url)
        
        # Configure connection and read timeouts from settings
        timeout = httpx.Timeout(
            timeout=15.0,
            connect=settings.EXTERNAL_AUTH_CONNECT_TIMEOUT,
            read=settings.EXTERNAL_AUTH_READ_TIMEOUT
        )
        
        try:
            async with httpx.AsyncClient(verify=ssl_ctx, http2=False) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout
                )
        except httpx.TimeoutException as e:
            logger.error("AUTH_PROVIDER_TIMEOUT: Outbound request timed out: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service is temporarily unavailable"
            )
        except (httpx.ConnectError, httpx.NetworkError) as e:
            logger.error("AUTH_PROVIDER_CONNECTION_FAILED: Connection to provider failed: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service is temporarily unavailable"
            )
        except httpx.HTTPError as e:
            logger.error("AUTH_PROVIDER_REQUEST_FAILED: Outbound request failed: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service is temporarily unavailable"
            )
            
        logger.info(
            "AUTH_PROVIDER_RESPONSE_RECEIVED: Received response status: %s, Content-Type: %s",
            response.status_code,
            response.headers.get("content-type", "")
        )
        
        if response.status_code == 403:
            logger.error("AUTH_PROVIDER_FORBIDDEN: External provider returned 403 Forbidden")
            # If it's a WAF block (HTML), raise 502 with client-safe message
            if "application/json" not in response.headers.get("content-type", "").lower():
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Authentication service is temporarily unavailable"
                )
            # Otherwise, treat as invalid credentials
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
            
        if response.status_code == 401:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
            
        if response.status_code >= 500:
            logger.error("AUTH_PROVIDER_SERVER_ERROR: Provider returned status: %d", response.status_code)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service is temporarily unavailable"
            )
            
        if response.status_code != 200:
            logger.error("AUTH_PROVIDER_UNEXPECTED_STATUS: Provider returned status: %d", response.status_code)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Authentication service is temporarily unavailable"
            )
            
        try:
            data = response.json()
        except (ValueError, TypeError) as e:
            logger.error("AUTH_PROVIDER_INVALID_RESPONSE: Malformed JSON received: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Authentication service is temporarily unavailable"
            )
            
        if not isinstance(data, dict) or data.get("status") == "failed" or "user" not in data:
            logger.error("AUTH_PROVIDER_INVALID_RESPONSE: Response layout invalid or login failed status")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
            
        user_data = data["user"]
        if not isinstance(user_data, dict):
            logger.error("AUTH_PROVIDER_INVALID_RESPONSE: Response 'user' field is not an object")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Authentication service is temporarily unavailable"
            )
            
        return user_data

    @classmethod
    def _request_headers(cls) -> dict:
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": settings.WORDPRESS_LOGIN_USER_AGENT,
            "Cache-Control": "no-cache",
            "Postman-Token": str(uuid.uuid4()),
            "Accept-Encoding": ACCEPT_ENCODING,
        }

    @classmethod
    async def authenticate_token(cls, token: str) -> dict:
        """
        Verify a provider-issued JWT (the one handed to the browser as ?token=...)
        and return the profile of the user it belongs to.

        The token is never trusted locally: the provider is asked whether it is
        still valid, and the identity is then read from the provider's own
        profile endpoint using that same token. A token that is expired, revoked
        or forged fails at the provider, not here.
        """
        normalized_token = token.strip()
        if not normalized_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired sign-in link",
            )

        headers = cls._request_headers()
        headers["Authorization"] = f"Bearer {normalized_token}"
        ssl_ctx = cls._create_ssl_context()
        timeout = httpx.Timeout(
            timeout=15.0,
            connect=settings.EXTERNAL_AUTH_CONNECT_TIMEOUT,
            read=settings.EXTERNAL_AUTH_READ_TIMEOUT,
        )

        logger.info("AUTH_SSO_STARTED: Verifying provider token with %s", settings.WORDPRESS_TOKEN_VALIDATE_URL)

        try:
            async with httpx.AsyncClient(verify=ssl_ctx, http2=False) as client:
                validation = await client.post(
                    settings.WORDPRESS_TOKEN_VALIDATE_URL,
                    headers=headers,
                    timeout=timeout,
                )
                if validation.status_code != 200:
                    logger.error(
                        "AUTH_SSO_TOKEN_REJECTED: Provider returned status %d for token validation",
                        validation.status_code,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid or expired sign-in link",
                    )

                try:
                    validation_body = validation.json()
                except (ValueError, TypeError):
                    logger.error("AUTH_SSO_INVALID_RESPONSE: Token validation returned malformed JSON")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Authentication service is temporarily unavailable",
                    )

                if not isinstance(validation_body, dict) or validation_body.get("code") != "jwt_auth_valid_token":
                    logger.error("AUTH_SSO_TOKEN_REJECTED: Provider did not confirm the token as valid")
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid or expired sign-in link",
                    )

                profile_response = await client.get(
                    settings.WORDPRESS_PROFILE_URL,
                    headers=headers,
                    timeout=timeout,
                )
        except HTTPException:
            raise
        except httpx.TimeoutException as e:
            logger.error("AUTH_SSO_PROVIDER_TIMEOUT: Outbound request timed out: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service is temporarily unavailable",
            )
        except (httpx.ConnectError, httpx.NetworkError) as e:
            logger.error("AUTH_SSO_PROVIDER_CONNECTION_FAILED: Connection to provider failed: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service is temporarily unavailable",
            )
        except httpx.HTTPError as e:
            logger.error("AUTH_SSO_PROVIDER_REQUEST_FAILED: Outbound request failed: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service is temporarily unavailable",
            )

        if profile_response.status_code in (401, 403):
            logger.error("AUTH_SSO_PROFILE_REJECTED: Provider refused the profile request")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired sign-in link",
            )

        if profile_response.status_code != 200:
            logger.error("AUTH_SSO_PROFILE_UNEXPECTED_STATUS: Provider returned status %d", profile_response.status_code)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Authentication service is temporarily unavailable",
            )

        try:
            profile_body = profile_response.json()
        except (ValueError, TypeError):
            logger.error("AUTH_SSO_INVALID_RESPONSE: Profile response was malformed JSON")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Authentication service is temporarily unavailable",
            )

        profile = profile_body.get("data") if isinstance(profile_body, dict) else None
        if not isinstance(profile, dict) or not profile.get("email"):
            logger.error("AUTH_SSO_INVALID_RESPONSE: Profile response omitted the user identity")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Authentication service is temporarily unavailable",
            )

        logger.info("AUTH_SSO_PROFILE_RECEIVED: Provider identity resolved for user_id %s", profile.get("user_id"))
        return profile

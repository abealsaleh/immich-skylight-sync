"""Skylight web portal API client.

This client interacts with the Skylight web portal at app.ourskylight.com.
API endpoints reverse-engineered from the mobile app.

Authentication flow (OAuth2 Authorization-Code + PKCE, as used by the current apps;
the legacy POST /api/sessions endpoint is retired):
1. GET /oauth/authorize -> web login form carrying a CSRF authenticity_token
2. POST /auth/session with email/password -> redirects to skylight-family://welcome?code=...
3. POST /oauth/token with the code + PKCE verifier -> access_token / refresh_token
4. Subsequent requests use "Authorization: Bearer <access_token>"
"""

import base64
import hashlib
import logging
import re
import secrets
import time
import uuid
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from models import SkylightPhoto

logger = logging.getLogger(__name__)

OAUTH_CLIENT_ID = "skylight-mobile"
OAUTH_SCOPE = "everything"
OAUTH_REDIRECT_URI = "skylight-family://welcome"
# The OAuth login pages are served to browsers; present a browser UA there.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CSRF_RE = re.compile(r'name="authenticity_token"[^>]*value="([^"]+)"')
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class SkylightError(Exception):
    """Exception raised for Skylight API errors."""

    pass


class SkylightAuthError(SkylightError):
    """Exception raised for authentication errors."""

    pass


class SkylightClient:
    """Client for interacting with the Skylight web portal API."""

    BASE_URL = "https://app.ourskylight.com"

    def __init__(
        self,
        email: str,
        password: str,
        frame_id: str,
        timeout: int = 30,
    ):
        """
        Initialize the Skylight client.

        Args:
            email: Skylight account email
            password: Skylight account password
            frame_id: Frame ID (from URL: app.ourskylight.com/frames/{frame_id}/messages)
            timeout: Request timeout in seconds
        """
        self.email = email
        self.password = password
        self.frame_id = frame_id
        self.timeout = timeout
        self.session = requests.Session()

        # Will be set after login
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: Optional[float] = None

        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def _token_valid(self) -> bool:
        """True if we hold an access token that isn't about to expire."""
        if not self.access_token:
            return False
        return self.expires_at is None or time.time() < self.expires_at - 60

    def _ensure_token(self) -> None:
        """Make sure we have a usable access token (refresh, else full login)."""
        if self._token_valid():
            return
        if self.refresh_token:
            try:
                self._refresh()
                return
            except SkylightError as e:
                logger.warning(f"Token refresh failed, logging in again: {e}")
        self.login()

    def _request(
        self,
        method: str,
        endpoint: str,
        authenticate: bool = True,
        **kwargs,
    ) -> requests.Response:
        """Make a request to the Skylight API."""
        url = f"{self.BASE_URL}{endpoint}"
        kwargs.setdefault("timeout", self.timeout)

        try:
            if authenticate:
                self._ensure_token()
                self.session.headers["Authorization"] = f"Bearer {self.access_token}"

            response = self.session.request(method, url, **kwargs)

            # Token may have been revoked server-side: log in again once and retry
            if response.status_code == 401 and authenticate:
                self.login()
                self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                response = self.session.request(method, url, **kwargs)

            if response.status_code == 401:
                raise SkylightAuthError(f"Authentication failed: {response.text}")

            response.raise_for_status()
            return response

        except requests.exceptions.HTTPError as e:
            logger.error(
                f"Skylight API error: {e.response.status_code} - {e.response.text}"
            )
            raise SkylightError(f"API request failed: {e}") from e
        except requests.exceptions.RequestException as e:
            logger.error(f"Skylight request error: {e}")
            raise SkylightError(f"Request failed: {e}") from e

    def _store_token(self, payload: dict) -> None:
        """Store tokens from an /oauth/token response."""
        if not payload.get("access_token"):
            raise SkylightAuthError("Token response did not contain an access_token")
        self.access_token = payload["access_token"]
        self.refresh_token = payload.get("refresh_token") or self.refresh_token
        created = payload.get("created_at")
        expires_in = payload.get("expires_in")
        if isinstance(created, (int, float)) and isinstance(expires_in, (int, float)):
            self.expires_at = float(created) + float(expires_in)
        else:
            self.expires_at = None

    def _token_request(self, data: dict) -> None:
        """POST to /oauth/token and store the resulting tokens."""
        response = requests.post(
            f"{self.BASE_URL}/oauth/token",
            data={"client_id": OAUTH_CLIENT_ID, **data},
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise SkylightAuthError(
                f"Token request failed: {response.status_code} - {response.text}"
            )
        self._store_token(response.json())

    def _refresh(self) -> None:
        """Exchange the refresh token for a new access token."""
        self._token_request(
            {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
        )
        logger.info("Refreshed Skylight access token")

    def login(self) -> bool:
        """
        Authenticate with Skylight via OAuth2 Authorization-Code + PKCE.

        Returns:
            True if authentication successful
        """
        self.access_token = self.refresh_token = self.expires_at = None
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = _b64url(secrets.token_bytes(18))

        # Separate session: the login pages use their own cookies and browser UA
        web = requests.Session()
        web.headers["User-Agent"] = BROWSER_UA

        def get(location: str) -> requests.Response:
            url = location if location.startswith("http") else self.BASE_URL + location
            return web.get(url, allow_redirects=False, timeout=self.timeout)

        try:
            # Step 1: load the login form (follow redirects manually)
            response = web.get(
                f"{self.BASE_URL}/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": OAUTH_CLIENT_ID,
                    "redirect_uri": OAUTH_REDIRECT_URI,
                    "scope": OAUTH_SCOPE,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "login",
                },
                allow_redirects=False,
                timeout=self.timeout,
            )
            for _ in range(10):
                if response.status_code not in _REDIRECT_CODES:
                    break
                response = get(response.headers["location"])

            csrf = _CSRF_RE.search(response.text)
            if not csrf:
                raise SkylightAuthError(
                    f"Could not load login form (HTTP {response.status_code}, no CSRF token)"
                )

            # Step 2: submit credentials, chase redirects to skylight-family://welcome
            response = web.post(
                f"{self.BASE_URL}/auth/session",
                data={
                    "authenticity_token": csrf.group(1),
                    "email": self.email,
                    "password": self.password,
                },
                allow_redirects=False,
                timeout=self.timeout,
            )
            location = response.headers.get("location")
            for _ in range(8):
                if not location or location.startswith("skylight-family:"):
                    break
                location = get(location).headers.get("location")

            if not (location and location.startswith("skylight-family:")):
                raise SkylightAuthError("Invalid email or password")

            query = parse_qs(urlparse(location).query)
            if query.get("state", [""])[0] != state:
                raise SkylightAuthError("OAuth state mismatch")
            code = query.get("code", [""])[0]
            if not code:
                raise SkylightAuthError("OAuth authorization code missing")

            # Step 3: exchange the code for tokens
            self._token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": OAUTH_REDIRECT_URI,
                    "code_verifier": verifier,
                }
            )

            logger.info("Successfully authenticated with Skylight")
            return True

        except SkylightAuthError:
            raise
        except Exception as e:
            logger.error(f"Login failed: {e}")
            raise SkylightAuthError(f"Login failed: {e}") from e

    def list_photos(
        self,
        page_token: str = "__START__",
    ) -> tuple[list[SkylightPhoto], Optional[str]]:
        """
        Get photos currently on the frame.

        Args:
            page_token: Pagination token, "__START__" for first page

        Returns:
            Tuple of (list of SkylightPhoto, next_page_token or None)
        """
        response = self._request(
            "GET",
            f"/api/frames/{self.frame_id}/messages",
            params={"page_token": page_token},
        )

        data = response.json()

        # Parse response - structure may vary
        messages = data.get("data", data.get("messages", []))
        if isinstance(messages, dict):
            messages = messages.get("messages", [])

        photos = [SkylightPhoto.from_api_response(m) for m in messages]

        # Get next page token if available
        next_token = data.get("meta", {}).get("next_page_token")

        return photos, next_token

    def get_all_photos(self) -> list[SkylightPhoto]:
        """
        Get all photos on the frame (handles pagination).

        Returns:
            Complete list of SkylightPhoto objects
        """
        all_photos = []
        page_token = "__START__"

        while page_token:
            photos, next_token = self.list_photos(page_token=page_token)
            all_photos.extend(photos)

            if not next_token or next_token == page_token:
                break

            page_token = next_token

            # Safety limit
            if len(all_photos) > 10000:
                logger.warning("Reached safety limit of 10000 photos")
                break

        return all_photos

    def upload_photo(
        self,
        photo_data: bytes,
        filename: str,
        caption: str = "",
    ) -> Optional[str]:
        """
        Upload a photo to the frame.

        Two-step process:
        1. POST /api/message_upload_urls to get pre-signed S3 URL
        2. PUT image bytes to S3 URL

        Args:
            photo_data: Raw image bytes
            filename: Filename for the photo (we embed Immich ID here)
            caption: Optional caption for the photo

        Returns:
            Skylight message ID if successful, None otherwise
        """
        # Determine file extension
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
        if ext not in ("jpg", "jpeg", "png", "gif"):
            ext = "jpg"

        # Generate a local file ID for tracking
        local_file_id = f"{uuid.uuid4().hex[:16]}.{ext}"

        # Step 1: Request pre-signed upload URL
        try:
            response = self._request(
                "POST",
                "/api/message_upload_urls",
                json={
                    "frame_ids": [self.frame_id],
                    "messages": [
                        {
                            "ext": ext,
                            "caption": caption,
                            "local_file_id": local_file_id,
                        }
                    ],
                },
            )

            data = response.json()
            upload_info = data["data"]["upload_urls"][0]
            upload_url = upload_info["url"]
            message_ids = upload_info.get("message_ids", [])

            logger.debug(f"Got upload URL for {filename}")

        except Exception as e:
            logger.error(f"Failed to get upload URL for {filename}: {e}")
            raise SkylightError(f"Failed to get upload URL: {e}") from e

        # Step 2: Upload to S3
        try:
            # Determine content type
            content_type = "application/octet-stream"

            s3_response = requests.put(
                upload_url,
                data=photo_data,
                headers={
                    "Content-Type": content_type,
                    "User-Agent": "okhttp/4.12.0",
                },
                timeout=120,
            )
            s3_response.raise_for_status()

            message_id = str(message_ids[0]) if message_ids else None
            logger.info(f"Uploaded photo: {filename} -> message_id={message_id}")
            return message_id

        except Exception as e:
            logger.error(f"Failed to upload {filename} to S3: {e}")
            raise SkylightError(f"S3 upload failed: {e}") from e

    def delete_photo(self, message_id: str) -> bool:
        """
        Delete a photo from the frame.

        Args:
            message_id: Skylight message ID to delete

        Returns:
            True if deletion successful or photo already gone (404)
        """
        try:
            self._request(
                "DELETE",
                f"/api/frames/{self.frame_id}/messages/{message_id}",
            )
            logger.info(f"Deleted photo: {message_id}")
            return True

        except SkylightError as e:
            # Treat 404 as success - photo is already gone
            if "404" in str(e):
                logger.info(f"Photo {message_id} already deleted (404)")
                return True
            logger.error(f"Failed to delete photo {message_id}: {e}")
            return False

    def delete_photos(self, message_ids: list[str]) -> tuple[int, int]:
        """
        Delete multiple photos from the frame.

        Args:
            message_ids: List of Skylight message IDs to delete

        Returns:
            Tuple of (successful_deletes, failed_deletes)
        """
        success_count = 0
        fail_count = 0

        for message_id in message_ids:
            if self.delete_photo(message_id):
                success_count += 1
            else:
                fail_count += 1

            # Rate limit to avoid overwhelming the API
            time.sleep(0.5)

        return success_count, fail_count

    def validate_connection(self) -> bool:
        """Validate that we can connect to Skylight with provided credentials."""
        try:
            self.login()
            return True
        except SkylightError as e:
            logger.error(f"Skylight connection check failed: {e}")
            return False

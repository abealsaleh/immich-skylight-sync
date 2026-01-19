"""Skylight web portal API client.

This client interacts with the Skylight web portal at app.ourskylight.com.
API endpoints reverse-engineered from the mobile app.

Authentication flow:
1. POST /api/sessions with email/password
2. Response contains user_id and token (atu_...)
3. Subsequent requests use Basic auth: base64(user_id:token)
"""

import base64
import logging
import time
import uuid
from typing import Optional

import requests

from models import SkylightPhoto

logger = logging.getLogger(__name__)


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
        self.user_id: Optional[str] = None
        self.token: Optional[str] = None
        self._authenticated = False

        # Set default headers matching the mobile app
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "SkylightMobile/1.95.2 (python-sync)",
            }
        )

    def _get_basic_auth_header(self) -> str:
        """Generate Basic Auth header value."""
        if not self.user_id or not self.token:
            raise SkylightAuthError("Not authenticated - call login() first")
        credentials = f"{self.user_id}:{self.token}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

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

        if authenticate:
            if not self._authenticated:
                self.login()
            self.session.headers["Authorization"] = self._get_basic_auth_header()

        try:
            response = self.session.request(method, url, **kwargs)

            if response.status_code == 401:
                raise SkylightAuthError("Authentication failed - check credentials")

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

    def login(self) -> bool:
        """
        Authenticate with Skylight.

        POST /api/sessions with email/password returns user_id and token.

        Returns:
            True if authentication successful
        """
        try:
            response = self.session.post(
                f"{self.BASE_URL}/api/sessions",
                json={"email": self.email, "password": self.password},
                timeout=self.timeout,
            )

            if response.status_code == 401:
                raise SkylightAuthError("Invalid email or password")

            response.raise_for_status()
            data = response.json()

            # Extract user_id and token from response
            # {"data": {"id": "123", "attributes": {"token": "atu_..."}}}
            self.user_id = data["data"]["id"]
            self.token = data["data"]["attributes"]["token"]
            self._authenticated = True

            logger.info(f"Successfully authenticated as user {self.user_id}")
            return True

        except SkylightAuthError:
            self._authenticated = False
            raise
        except Exception as e:
            logger.error(f"Login failed: {e}")
            self._authenticated = False
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
        except (SkylightError, SkylightAuthError):
            return False

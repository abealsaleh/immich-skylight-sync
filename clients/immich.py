"""Immich API client."""

import logging
from typing import Optional

import requests

from models import ImmichAlbum, ImmichAsset

logger = logging.getLogger(__name__)


class ImmichError(Exception):
    """Exception raised for Immich API errors."""

    pass


class ImmichClient:
    """Client for interacting with the Immich API."""

    def __init__(self, url: str, api_key: str, timeout: int = 30):
        """
        Initialize the Immich client.

        Args:
            url: Base URL of the Immich server (e.g., http://192.168.1.x:2283)
            api_key: API key for authentication
            timeout: Request timeout in seconds
        """
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-api-key": api_key,
                "Accept": "application/json",
            }
        )

    def _request(
        self, method: str, endpoint: str, **kwargs
    ) -> requests.Response:
        """Make an authenticated request to the Immich API."""
        url = f"{self.url}/api{endpoint}"
        kwargs.setdefault("timeout", self.timeout)

        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            logger.error(f"Immich API error: {e.response.status_code} - {e.response.text}")
            raise ImmichError(f"API request failed: {e}") from e
        except requests.exceptions.RequestException as e:
            logger.error(f"Immich request error: {e}")
            raise ImmichError(f"Request failed: {e}") from e

    def get_server_info(self) -> dict:
        """Get server information to verify connectivity."""
        response = self._request("GET", "/server/about")
        return response.json()

    def validate_connection(self) -> bool:
        """Validate that we can connect to Immich with the provided credentials."""
        try:
            # Use get_albums instead of server info - only requires album:read permission
            self.get_albums()
            return True
        except ImmichError:
            return False

    def get_albums(self) -> list[ImmichAlbum]:
        """
        Get all albums.

        Returns:
            List of ImmichAlbum objects (without full asset details)
        """
        response = self._request("GET", "/albums")
        albums = response.json()
        return [ImmichAlbum.from_api_response(a) for a in albums]

    def get_album(self, album_id: str) -> ImmichAlbum:
        """
        Get a specific album with all its assets.

        Args:
            album_id: The album's UUID

        Returns:
            ImmichAlbum with populated assets list
        """
        response = self._request("GET", f"/albums/{album_id}")
        return ImmichAlbum.from_api_response(response.json())

    def get_album_by_name(self, name: str) -> Optional[ImmichAlbum]:
        """
        Find an album by name.

        Args:
            name: Album name to search for (case-sensitive)

        Returns:
            ImmichAlbum with assets if found, None otherwise
        """
        albums = self.get_albums()
        for album in albums:
            if album.name == name:
                return self.get_album(album.id)
        return None

    def download_asset(self, asset_id: str) -> bytes:
        """
        Download the original file for an asset.

        Args:
            asset_id: The asset's UUID

        Returns:
            Raw bytes of the original file
        """
        response = self._request(
            "GET",
            f"/assets/{asset_id}/original",
            timeout=120,  # Longer timeout for downloads
        )
        return response.content

    def download_asset_thumbnail(
        self, asset_id: str, size: str = "preview"
    ) -> bytes:
        """
        Download a thumbnail for an asset.

        Args:
            asset_id: The asset's UUID
            size: Thumbnail size - "thumbnail" (small) or "preview" (larger)

        Returns:
            Raw bytes of the thumbnail
        """
        response = self._request(
            "GET",
            f"/assets/{asset_id}/thumbnail",
            params={"size": size},
        )
        return response.content

    def get_asset_info(self, asset_id: str) -> ImmichAsset:
        """
        Get information about a specific asset.

        Args:
            asset_id: The asset's UUID

        Returns:
            ImmichAsset with full details
        """
        response = self._request("GET", f"/assets/{asset_id}")
        return ImmichAsset.from_api_response(response.json())

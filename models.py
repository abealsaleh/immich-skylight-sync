"""Data models for Immich-Skylight sync service."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ImmichAsset:
    """Represents a photo/video asset from Immich."""

    id: str
    original_filename: str
    file_created_at: Optional[datetime] = None
    type: str = "IMAGE"  # IMAGE or VIDEO
    checksum: Optional[str] = None

    @classmethod
    def from_api_response(cls, data: dict) -> "ImmichAsset":
        """Create an ImmichAsset from Immich API response."""
        file_created_at = None
        if data.get("fileCreatedAt"):
            try:
                file_created_at = datetime.fromisoformat(
                    data["fileCreatedAt"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        return cls(
            id=data["id"],
            original_filename=data.get("originalFileName", "unknown.jpg"),
            file_created_at=file_created_at,
            type=data.get("type", "IMAGE"),
            checksum=data.get("checksum"),
        )


@dataclass
class ImmichAlbum:
    """Represents an album from Immich."""

    id: str
    name: str
    asset_count: int = 0
    assets: list[ImmichAsset] = field(default_factory=list)

    @classmethod
    def from_api_response(cls, data: dict) -> "ImmichAlbum":
        """Create an ImmichAlbum from Immich API response."""
        assets = []
        if "assets" in data:
            assets = [ImmichAsset.from_api_response(a) for a in data["assets"]]

        return cls(
            id=data["id"],
            name=data.get("albumName", "Untitled"),
            asset_count=data.get("assetCount", len(assets)),
            assets=assets,
        )


@dataclass
class SkylightPhoto:
    """Represents a photo on a Skylight frame."""

    id: str
    asset_key: Optional[str] = None
    asset_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    created_at: Optional[datetime] = None
    immich_id: Optional[str] = None  # Extracted from caption or local tracking
    caption: Optional[str] = None

    @classmethod
    def from_api_response(cls, data: dict) -> "SkylightPhoto":
        """Create a SkylightPhoto from Skylight API response.

        API response format:
        {
            "id": "123",
            "type": "message_status",
            "attributes": {
                "id": 123,
                "status": "downloaded",
                "asset_type": "photo",
                "created_at": "2026-01-19T15:10:32.068Z",
                "thumbnail_url": "...",
                "asset_url": "..."
            }
        }
        """
        # Handle nested attributes structure
        attrs = data.get("attributes", data)

        created_at = None
        created_str = attrs.get("created_at") or data.get("created_at")
        if created_str:
            try:
                created_at = datetime.fromisoformat(
                    created_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # Get the ID from either location
        photo_id = str(data.get("id") or attrs.get("id") or "")

        # Try to extract asset key from URL
        asset_url = attrs.get("asset_url")
        asset_key = None
        if asset_url:
            # URL format: https://...cloudfront.net/{key}.jpg?...
            try:
                path = asset_url.split("?")[0]
                asset_key = path.split("/")[-1].rsplit(".", 1)[0]
            except (IndexError, AttributeError):
                pass

        # Caption might contain our Immich ID (we'll embed it there)
        caption = attrs.get("caption")
        immich_id = None
        if caption and caption.startswith("immich:"):
            # Format: "immich:{uuid}"
            potential_id = caption[7:].strip()
            if len(potential_id) == 36 and potential_id.count("-") == 4:
                immich_id = potential_id

        return cls(
            id=photo_id,
            asset_key=asset_key,
            asset_url=asset_url,
            thumbnail_url=attrs.get("thumbnail_url"),
            created_at=created_at,
            immich_id=immich_id,
            caption=caption,
        )


@dataclass
class SyncState:
    """Represents the sync state of a photo."""

    immich_id: str
    skylight_id: str
    synced_at: datetime
    checksum: Optional[str] = None


@dataclass
class Config:
    """Application configuration."""

    # Immich settings
    immich_url: str
    immich_api_key: str
    immich_albums: list[str]

    # Skylight settings
    skylight_email: str
    skylight_password: str
    skylight_frame_id: str

    # Sync settings
    sync_interval_minutes: int = 60
    delete_removed: bool = True
    dry_run: bool = False

    @classmethod
    def from_yaml(cls, data: dict) -> "Config":
        """Create Config from parsed YAML data."""
        immich = data.get("immich", {})
        skylight = data.get("skylight", {})
        sync = data.get("sync", {})

        return cls(
            immich_url=immich.get("url", ""),
            immich_api_key=immich.get("api_key", ""),
            immich_albums=immich.get("albums", []),
            skylight_email=skylight.get("email", ""),
            skylight_password=skylight.get("password", ""),
            skylight_frame_id=str(skylight.get("frame_id", "")),
            sync_interval_minutes=sync.get("interval_minutes", 60),
            delete_removed=sync.get("delete_removed", True),
            dry_run=sync.get("dry_run", False),
        )

    def validate(self) -> list[str]:
        """Validate configuration, return list of errors."""
        errors = []

        if not self.immich_url:
            errors.append("immich.url is required")
        if not self.immich_api_key:
            errors.append("immich.api_key is required")
        if not self.immich_albums:
            errors.append("immich.albums must contain at least one album name")

        if not self.skylight_email:
            errors.append("skylight.email is required")
        if not self.skylight_password:
            errors.append("skylight.password is required")
        if not self.skylight_frame_id:
            errors.append("skylight.frame_id is required")

        return errors

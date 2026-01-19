"""Sync logic between Immich and Skylight."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from clients import ImmichClient, SkylightClient
from models import Config, ImmichAsset, SkylightPhoto
from state import StateStore

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    photos_added: int = 0
    photos_removed: int = 0
    photos_skipped: int = 0
    errors: int = 0
    error_messages: list[str] = None

    def __post_init__(self):
        if self.error_messages is None:
            self.error_messages = []


def build_immich_caption(asset: ImmichAsset) -> str:
    """
    Build a caption for Skylight that embeds the Immich ID.

    Format: immich:{uuid}

    This allows us to identify which Skylight photos correspond to which
    Immich assets by parsing the caption. Note: captions require Skylight Plus.
    For basic accounts, we rely on the SQLite state database.
    """
    return f"immich:{asset.id}"


def extract_immich_id_from_caption(caption: Optional[str]) -> Optional[str]:
    """
    Extract the Immich asset ID from a Skylight caption.

    Args:
        caption: Caption in format "immich:{uuid}"

    Returns:
        Immich ID if found, None otherwise
    """
    if not caption:
        return None

    if not caption.startswith("immich:"):
        return None

    potential_id = caption[7:].strip()

    # Immich IDs are UUIDs (36 chars with dashes)
    if len(potential_id) == 36 and potential_id.count("-") == 4:
        return potential_id

    return None


def sync(
    immich: ImmichClient,
    skylight: SkylightClient,
    config: Config,
    state: StateStore,
) -> SyncResult:
    """
    Perform a sync between Immich albums and Skylight.

    This function:
    1. Gets the desired state from configured Immich albums
    2. Gets the current state from Skylight
    3. Calculates what needs to be added/removed
    4. Applies changes (unless dry_run is enabled)

    Args:
        immich: Initialized Immich client
        skylight: Initialized Skylight client
        config: Sync configuration
        state: State store for tracking synced photos

    Returns:
        SyncResult with statistics
    """
    result = SyncResult()

    # Start tracking this sync run
    run_id = state.start_sync_run()

    try:
        # Step 1: Get desired state from Immich albums
        logger.info(f"Fetching photos from {len(config.immich_albums)} album(s)...")
        desired_photos: dict[str, ImmichAsset] = {}

        for album_name in config.immich_albums:
            album = immich.get_album_by_name(album_name)
            if not album:
                msg = f"Album not found: {album_name}"
                logger.warning(msg)
                result.error_messages.append(msg)
                result.errors += 1
                continue

            logger.info(f"  Album '{album_name}': {len(album.assets)} photos")
            for asset in album.assets:
                # Only sync images, not videos
                if asset.type == "IMAGE":
                    desired_photos[asset.id] = asset

        logger.info(f"Total desired photos: {len(desired_photos)}")

        # Step 2: Get current state from Skylight
        logger.info("Fetching current Skylight photos...")
        current_photos = skylight.get_all_photos()
        logger.info(f"Current Skylight photos: {len(current_photos)}")

        # Build lookup maps using state database as primary source
        # (captions may not be available on basic Skylight accounts)
        current_by_immich_id: dict[str, SkylightPhoto] = {}
        current_by_skylight_id: dict[str, SkylightPhoto] = {p.id: p for p in current_photos}
        current_without_immich_id: list[SkylightPhoto] = []

        # First, check state database for known mappings
        synced_states = state.get_all_states()
        known_skylight_ids = set()

        for sync_state in synced_states:
            if sync_state.skylight_id in current_by_skylight_id:
                # Photo still exists on Skylight
                photo = current_by_skylight_id[sync_state.skylight_id]
                photo.immich_id = sync_state.immich_id
                current_by_immich_id[sync_state.immich_id] = photo
                known_skylight_ids.add(sync_state.skylight_id)
            else:
                # Photo was deleted from Skylight - clean up state
                state.remove_sync_state(sync_state.immich_id)

        # Also check captions for any photos we might have missed
        for photo in current_photos:
            if photo.id in known_skylight_ids:
                continue

            # Try to get Immich ID from caption
            immich_id = photo.immich_id or extract_immich_id_from_caption(photo.caption)

            if immich_id:
                photo.immich_id = immich_id
                current_by_immich_id[immich_id] = photo
                # Update state database with this mapping
                state.save_sync_state(immich_id, photo.id)
            else:
                # Photos not managed by this sync (uploaded manually)
                current_without_immich_id.append(photo)

        logger.info(
            f"  Managed by sync: {len(current_by_immich_id)}, "
            f"Unmanaged: {len(current_without_immich_id)}"
        )

        # Step 3: Calculate diff
        to_add = [
            asset
            for asset_id, asset in desired_photos.items()
            if asset_id not in current_by_immich_id
        ]

        to_remove: list[SkylightPhoto] = []
        if config.delete_removed:
            to_remove = [
                photo
                for photo in current_photos
                if photo.immich_id and photo.immich_id not in desired_photos
            ]

        logger.info(f"Changes needed: +{len(to_add)} / -{len(to_remove)}")

        if config.dry_run:
            logger.info("DRY RUN - no changes will be made")
            if to_add:
                logger.info("Would add:")
                for asset in to_add[:10]:  # Show first 10
                    logger.info(f"  - {asset.original_filename}")
                if len(to_add) > 10:
                    logger.info(f"  ... and {len(to_add) - 10} more")

            if to_remove:
                logger.info("Would remove:")
                for photo in to_remove[:10]:
                    logger.info(f"  - {photo.filename}")
                if len(to_remove) > 10:
                    logger.info(f"  ... and {len(to_remove) - 10} more")

            result.photos_skipped = len(to_add) + len(to_remove)
            state.complete_sync_run(run_id, status="dry_run")
            return result

        # Step 4: Apply changes - Add new photos
        for i, asset in enumerate(to_add):
            logger.info(
                f"Uploading [{i + 1}/{len(to_add)}]: {asset.original_filename}"
            )
            try:
                # Download from Immich
                photo_data = immich.download_asset(asset.id)

                # Upload to Skylight (tracking via SQLite database)
                skylight_id = skylight.upload_photo(
                    photo_data,
                    asset.original_filename,
                )

                if skylight_id:
                    # Save state mapping (primary tracking method)
                    state.save_sync_state(
                        asset.id, skylight_id, checksum=asset.checksum
                    )
                    result.photos_added += 1
                else:
                    msg = f"Upload succeeded but no ID returned for {asset.original_filename}"
                    logger.warning(msg)
                    result.error_messages.append(msg)
                    result.errors += 1

                # Rate limit
                time.sleep(1)

            except Exception as e:
                msg = f"Failed to upload {asset.original_filename}: {e}"
                logger.error(msg)
                result.error_messages.append(msg)
                result.errors += 1

        # Step 5: Apply changes - Remove deleted photos
        for i, photo in enumerate(to_remove):
            photo_desc = f"id={photo.id}" + (f" (immich:{photo.immich_id})" if photo.immich_id else "")
            logger.info(
                f"Removing [{i + 1}/{len(to_remove)}]: {photo_desc}"
            )
            try:
                if skylight.delete_photo(photo.id):
                    # Remove from state
                    if photo.immich_id:
                        state.remove_sync_state(photo.immich_id)
                    result.photos_removed += 1
                else:
                    msg = f"Failed to delete photo {photo_desc}"
                    logger.warning(msg)
                    result.error_messages.append(msg)
                    result.errors += 1

                # Rate limit
                time.sleep(0.5)

            except Exception as e:
                msg = f"Failed to delete photo {photo_desc}: {e}"
                logger.error(msg)
                result.error_messages.append(msg)
                result.errors += 1

        # Complete sync run
        status = "completed" if result.errors == 0 else "partial"
        state.complete_sync_run(
            run_id,
            photos_added=result.photos_added,
            photos_removed=result.photos_removed,
            errors=result.errors,
            status=status,
        )

        logger.info(
            f"Sync complete: +{result.photos_added} / -{result.photos_removed} "
            f"({result.errors} errors)"
        )

        return result

    except Exception as e:
        logger.exception(f"Sync failed: {e}")
        state.complete_sync_run(run_id, errors=1, status="failed")
        result.errors += 1
        result.error_messages.append(str(e))
        return result

#!/usr/bin/env python3
"""
Immich to Skylight Photo Sync Service

Syncs photos from selected Immich albums to a Skylight digital picture frame.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import schedule
import yaml

from clients import ImmichClient, SkylightClient
from models import Config
from state import StateStore
from sync import sync

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def load_config(config_path: str) -> Config:
    """Load configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    config = Config.from_yaml(data)

    # Validate configuration
    errors = config.validate()
    if errors:
        for error in errors:
            logger.error(f"Config error: {error}")
        raise ValueError("Invalid configuration")

    return config


def run_sync(config: Config, state: StateStore) -> None:
    """Run a single sync operation."""
    logger.info("Starting sync...")

    try:
        # Initialize clients
        immich = ImmichClient(config.immich_url, config.immich_api_key)
        skylight = SkylightClient(
            config.skylight_email,
            config.skylight_password,
            config.skylight_frame_id,
        )

        # Validate connections
        logger.info("Validating Immich connection...")
        if not immich.validate_connection():
            logger.error("Failed to connect to Immich")
            return

        logger.info("Validating Skylight connection...")
        if not skylight.validate_connection():
            logger.error("Failed to connect to Skylight")
            return

        # Run sync
        result = sync(immich, skylight, config, state)

        if result.errors > 0:
            logger.warning(
                f"Sync completed with {result.errors} error(s): "
                f"+{result.photos_added} / -{result.photos_removed}"
            )
        else:
            logger.info(
                f"Sync completed successfully: "
                f"+{result.photos_added} / -{result.photos_removed}"
            )

    except Exception as e:
        logger.exception(f"Sync failed: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync photos from Immich albums to Skylight frame"
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run sync once and exit (don't schedule)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Override dry_run from command line
    if args.dry_run:
        config.dry_run = True

    # Initialize state store
    state = StateStore()

    if args.once:
        # Run once and exit
        run_sync(config, state)
    else:
        # Run immediately, then schedule periodic syncs
        logger.info(
            f"Starting sync service (interval: {config.sync_interval_minutes} minutes)"
        )

        run_sync(config, state)

        schedule.every(config.sync_interval_minutes).minutes.do(
            run_sync, config, state
        )

        logger.info("Sync service running. Press Ctrl+C to stop.")

        try:
            while True:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
        except KeyboardInterrupt:
            logger.info("Shutting down...")


if __name__ == "__main__":
    main()

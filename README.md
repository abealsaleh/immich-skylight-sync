# Immich to Skylight Photo Sync

Syncs photos from selected Immich albums to a Skylight digital picture frame.

## Features

- **Two-way sync**: Add/remove photos based on album contents
- **Multi-album support**: Configure one or more Immich albums
- **Automatic deduplication**: Photos in multiple albums are synced once
- **Tracking**: SQLite database tracks sync state
- **Docker deployment**: Easy deployment as a container
- **Dry run mode**: Preview changes before applying

## Setup

### 1. Create Immich API Key

Generate an API key in Immich with **read-only** permissions:

1. Open Immich and click your avatar → **Account Settings**
2. Go to **API Keys** → **New API Key**
3. Name it something like "Skylight Sync (Read-Only)"
4. Select **only** these permissions:
   - `album:read` - Read albums
   - `asset:read` - Read assets
   - `asset:download` - Download original photos

**Permissions NOT needed** (leave unchecked):
- ❌ Any write/create/delete permissions
- ❌ Library management
- ❌ User/Admin access
- ❌ Person/Face recognition
- ❌ Partner sharing

This follows the principle of least privilege - the sync service only reads from Immich and cannot modify or delete anything.

### 2. Get Skylight Frame ID

1. Log in to `https://app.ourskylight.com`
2. Navigate to your frame
3. The URL will be: `app.ourskylight.com/frames/{frame_id}/messages`
4. Copy the `frame_id` number

### 3. Configuration

Copy the example config and fill in your details:

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml`:

```yaml
immich:
  url: "http://192.168.1.x:2283"
  api_key: "your-immich-api-key"
  albums:
    - "Family Favorites"
    - "Vacation 2024"

skylight:
  email: "your-skylight-email@example.com"
  password: "your-skylight-password"
  frame_id: "123456"  # From URL: app.ourskylight.com/frames/{frame_id}/messages

sync:
  interval_minutes: 60
  delete_removed: true
  dry_run: false
```

### 4. Run

#### Local Python

```bash
# Install dependencies
pip install -r requirements.txt

# Run once (dry run to preview changes)
python main.py --once --dry-run

# Run once (apply changes)
python main.py --once

# Run as service (syncs every interval)
python main.py
```

#### Docker

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

## How It Works

### Photo Identity Tracking

Photo mappings between Immich and Skylight are tracked in a **SQLite database** at `data/sync_state.db`. This allows the sync service to know which Skylight photos correspond to which Immich assets without requiring Skylight Plus features.

### Sync Logic

1. Fetch photos from all configured Immich albums (deduplicated)
2. Fetch current photos from Skylight
3. Calculate diff (what to add/remove)
4. Apply changes with rate limiting

### What Gets Synced

- Only **images** are synced (videos are skipped)
- Photos removed from ALL configured albums are deleted from Skylight
- Manually uploaded Skylight photos are preserved (not deleted)

## Project Structure

```
immich-skylight-sync/
├── config.yaml           # Your configuration
├── main.py               # Entry point and scheduler
├── sync.py               # Sync logic
├── models.py             # Data models
├── state.py              # SQLite state tracking
├── clients/
│   ├── __init__.py
│   ├── immich.py         # Immich API client
│   └── skylight.py       # Skylight web portal client
├── data/
│   └── sync_state.db     # SQLite database (auto-created)
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## Troubleshooting

### "Album not found"

Album names are case-sensitive. Check the exact name in Immich.

### "Authentication failed"

For Skylight, verify:
- Email and password are correct
- The API endpoints match what you captured (may need to update `clients/skylight.py`)

### Photos not appearing on frame

Skylight may have a delay before photos appear on the physical frame. Check `app.ourskylight.com` to verify uploads succeeded.

## License

MIT

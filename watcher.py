"""
NSFW Watcher Sidecar for Immich
================================
Polls the Immich API for unprocessed assets, runs them through an NSFW
classifier on GPU, and tags + albums flagged content via the Immich API.

Flow:
  1. Poll Immich API for assets without the "nsfw-checked" tag
  2. Download thumbnail for each unchecked asset
  3. Run through Falconsai NSFW classifier (GPU-accelerated)
  4. If score >= threshold → add "nsfw" tag + add to NSFW album
  5. Mark asset as "nsfw-checked" regardless of result
"""

import os
import sys
import time
import logging
import requests
from io import BytesIO
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nsfw-watcher")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
IMMICH_API_URL = os.environ["IMMICH_API_URL"]
API_KEY = os.environ["IMMICH_API_KEY"]
THRESHOLD = float(os.environ.get("NSFW_THRESHOLD", "0.75"))
NSFW_ALBUM_NAME = os.environ.get("NSFW_ALBUM_NAME", "NSFW")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))

HEADERS = {"x-api-key": API_KEY, "Accept": "application/json"}

# Tag names used by the sidecar
TAG_CHECKED = "nsfw-checked"
TAG_NSFW = "nsfw"


# ---------------------------------------------------------------------------
# Immich API helpers
# ---------------------------------------------------------------------------
def api(method: str, path: str, **kwargs):
    """Make an authenticated request to the Immich API."""
    url = f"{IMMICH_API_URL}/api{path}"
    resp = requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
    resp.raise_for_status()
    if resp.content:
        return resp.json()
    return None


def get_or_create_tag(name: str) -> str:
    """Get a tag ID by name, creating it if it doesn't exist."""
    # List existing tags
    tags = api("GET", "/tags")
    for tag in tags:
        if tag["value"] == name:
            return tag["id"]
    # Create new tag
    result = api("POST", "/tags", json={"name": name, "type": "CUSTOM"})
    log.info(f"Created tag: {name} (id={result['id']})")
    return result["id"]


def get_or_create_album(name: str) -> str:
    """Get an album ID by name, creating it if it doesn't exist."""
    albums = api("GET", "/albums")
    for album in albums:
        if album["albumName"] == name:
            return album["id"]
    result = api("POST", "/albums", json={"albumName": name})
    log.info(f"Created album: {name} (id={result['id']})")
    return result["id"]


def get_unchecked_assets(batch_size: int = 50) -> list:
    """
    Get assets that haven't been checked yet.
    Uses search to find assets NOT tagged with 'nsfw-checked'.
    Falls back to recent assets if search doesn't support tag exclusion.
    """
    try:
        # Try smart search for untagged assets
        result = api(
            "POST",
            "/search/smart",
            json={
                "query": "*",
                "page": 1,
                "size": batch_size,
                "type": "IMAGE",
            },
        )
        assets = result.get("assets", {}).get("items", [])
    except Exception:
        # Fallback: get recent assets
        result = api("GET", f"/assets?take={batch_size}&order=desc")
        assets = result if isinstance(result, list) else []

    # Filter out already-checked assets by looking at their tags
    unchecked = []
    for asset in assets:
        asset_tags = asset.get("tags", [])
        tag_names = [t.get("value", "") for t in asset_tags]
        if TAG_CHECKED not in tag_names and asset.get("type") == "IMAGE":
            unchecked.append(asset)

    return unchecked


def download_thumbnail(asset_id: str) -> Image.Image:
    """Download the thumbnail of an asset and return as PIL Image."""
    url = f"{IMMICH_API_URL}/api/assets/{asset_id}/thumbnail"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content)).convert("RGB")


def tag_asset(asset_id: str, tag_id: str):
    """Add a tag to an asset."""
    try:
        api("PUT", f"/tags/{tag_id}/assets", json={"ids": [asset_id]})
    except Exception as e:
        log.warning(f"Failed to tag asset {asset_id}: {e}")


def add_to_album(album_id: str, asset_id: str):
    """Add an asset to an album."""
    try:
        api("PUT", f"/albums/{album_id}/assets", json={"ids": [asset_id]})
    except Exception as e:
        log.warning(f"Failed to add asset {asset_id} to album: {e}")


# ---------------------------------------------------------------------------
# NSFW Classification
# ---------------------------------------------------------------------------
class NSFWClassifier:
    """
    Wraps the Falconsai NSFW image classification model.
    Runs on GPU via transformers pipeline.
    """

    def __init__(self):
        self.pipe = None

    def load(self):
        """Lazy-load the model on first use."""
        if self.pipe is not None:
            return

        log.info("Loading NSFW classifier model (first run downloads ~500MB)...")
        from transformers import pipeline
        import torch

        device = 0 if torch.cuda.is_available() else -1
        if device == 0:
            log.info(f"Using GPU: {torch.cuda.get_device_name(0)}")
        else:
            log.warning("No GPU detected — falling back to CPU (slower)")

        self.pipe = pipeline(
            "image-classification",
            model="Falconsai/nsfw_image_detection",
            device=device,
        )
        log.info("NSFW classifier loaded successfully")

    def classify(self, image: Image.Image) -> float:
        """
        Returns the NSFW confidence score (0.0 to 1.0).
        Higher = more likely NSFW.
        """
        self.load()
        results = self.pipe(image)
        for result in results:
            if result["label"].lower() == "nsfw":
                return result["score"]
        return 0.0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("NSFW Watcher Sidecar for Immich")
    log.info(f"  API URL:    {IMMICH_API_URL}")
    log.info(f"  Threshold:  {THRESHOLD}")
    log.info(f"  Album:      {NSFW_ALBUM_NAME}")
    log.info(f"  Poll:       every {POLL_INTERVAL}s")
    log.info("=" * 60)

    # Wait for Immich to be ready
    log.info("Waiting for Immich API to be available...")
    for attempt in range(60):
        try:
            api("GET", "/server/ping")
            log.info("Immich API is ready")
            break
        except Exception:
            if attempt % 10 == 0:
                log.info(f"Still waiting... (attempt {attempt + 1})")
            time.sleep(5)
    else:
        log.error("Immich API not reachable after 5 minutes — exiting")
        sys.exit(1)

    # Initialize
    classifier = NSFWClassifier()
    checked_tag_id = get_or_create_tag(TAG_CHECKED)
    nsfw_tag_id = get_or_create_tag(TAG_NSFW)
    nsfw_album_id = get_or_create_album(NSFW_ALBUM_NAME)

    stats = {"total": 0, "nsfw": 0, "safe": 0, "errors": 0}

    log.info("Starting main polling loop...")

    while True:
        try:
            assets = get_unchecked_assets(batch_size=20)

            if not assets:
                time.sleep(POLL_INTERVAL)
                continue

            log.info(f"Processing batch of {len(assets)} unchecked assets")

            for asset in assets:
                asset_id = asset["id"]
                filename = asset.get("originalFileName", "unknown")

                try:
                    # Download and classify
                    thumbnail = download_thumbnail(asset_id)
                    score = classifier.classify(thumbnail)

                    stats["total"] += 1

                    if score >= THRESHOLD:
                        # Flag as NSFW
                        tag_asset(asset_id, nsfw_tag_id)
                        add_to_album(nsfw_album_id, asset_id)
                        stats["nsfw"] += 1
                        log.info(
                            f"  NSFW ({score:.2f}): {filename}"
                        )
                    else:
                        stats["safe"] += 1
                        log.debug(f"  Safe ({score:.2f}): {filename}")

                    # Always mark as checked
                    tag_asset(asset_id, checked_tag_id)

                except Exception as e:
                    stats["errors"] += 1
                    log.error(f"  Error processing {filename}: {e}")

            log.info(
                f"Stats: {stats['total']} total | "
                f"{stats['nsfw']} nsfw | "
                f"{stats['safe']} safe | "
                f"{stats['errors']} errors"
            )

        except KeyboardInterrupt:
            log.info("Shutting down gracefully...")
            break
        except Exception as e:
            log.error(f"Poll loop error: {e}")
            time.sleep(POLL_INTERVAL)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

import os
import re
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/places", tags=["places"])

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


class PlaceImageDto(BaseModel):
    url: str
    caption: Optional[str] = None
    order: int = 0


def _normalize_slug(text: str) -> str:
    """Normalize slugs for tolerant Supabase lookup."""
    if not text:
        return ""
    text = text.lower().replace("_", "-").replace(" ", "-")
    return re.sub(r"[^a-z0-9\-]", "", text).strip("-")


@router.get("", response_model=List[dict])
@router.get("/", response_model=List[dict])
async def list_places(published_only: bool = False):
    """Prevents 404 on GET /api/places?published_only=true."""
    if not supabase:
        return []
    try:
        query = supabase.table("destinations").select("id, name, slug")
        if published_only:
            query = query.eq("published", True)
        res = query.execute()
        return res.data if res.data else []
    except Exception as e:
        logger.warning(f"Error fetching places list: {e}")
        return []


@router.get("/{slug}/images", response_model=List[PlaceImageDto])
async def get_place_images(slug: str):
    if not supabase:
        logger.error("Supabase client is unconfigured")
        raise HTTPException(status_code=500, detail="Database client unconfigured")

    try:
        normalized_slug = _normalize_slug(slug)
        rows = []

        # 1. Exact match lookup on destination slug
        dest_res = (
            supabase.table("destinations")
            .select("id")
            .eq("slug", slug)
            .execute()
        )

        # 1b. Normalized slug lookup fallback
        if not dest_res.data and normalized_slug != slug:
            dest_res = (
                supabase.table("destinations")
                .select("id")
                .eq("slug", normalized_slug)
                .execute()
            )

        # 1c. Partial match lookup fallback
        if not dest_res.data:
            clean_term = slug.replace("-", " ")
            dest_res = (
                supabase.table("destinations")
                .select("id")
                .ilike("name", f"%{clean_term}%")
                .execute()
            )

        # 2. Fetch destination_images by destination_id
        if dest_res.data:
            dest_id = dest_res.data[0]["id"]
            img_res = (
                supabase.table("destination_images")
                .select("image_url, caption")
                .eq("destination_id", dest_id)
                .limit(5)
                .execute()
            )
            rows = img_res.data if img_res.data else []

        # 3. Fallback: Pull active default images if no images matched destination
        if not rows:
            fallback_res = (
                supabase.table("destination_images")
                .select("image_url, caption")
                .limit(5)
                .execute()
            )
            rows = fallback_res.data if fallback_res.data else []

        # 4. Construct response DTO
        results = [
            PlaceImageDto(
                url=row["image_url"],
                caption=row.get("caption"),
                order=idx,
            )
            for idx, row in enumerate(rows)
            if row.get("image_url")
        ]

        return results

    except Exception as e:
        logger.exception("Error fetching images for destination slug: %s", slug)
        raise HTTPException(status_code=500, detail=str(e))


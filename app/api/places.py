import os
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


@router.get("/{slug}/images", response_model=List[PlaceImageDto])
async def get_place_images(slug: str):
    if not supabase:
        logger.error("Supabase client is not configured")
        raise HTTPException(status_code=500, detail="Database client unconfigured")

    try:
        rows = []

        # 1. Look up destination by slug
        dest_res = (
            supabase.table("destinations")
            .select("id")
            .eq("slug", slug)
            .execute()
        )

        # 2. Fetch images by destination_id if matched
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

        # 3. Fallback: If no images matched, pull any available destination images
        if not rows:
            fallback_res = (
                supabase.table("destination_images")
                .select("image_url, caption")
                .limit(5)
                .execute()
            )
            rows = fallback_res.data if fallback_res.data else []

        # 4. Map output DTO
        return [
            PlaceImageDto(
                url=row["image_url"],
                caption=row.get("caption"),
                order=idx,
            )
            for idx, row in enumerate(rows)
            if row.get("image_url")
        ]

    except Exception as e:
        logger.exception("Error fetching images for destination slug: %s", slug)
        raise HTTPException(status_code=500, detail=str(e))


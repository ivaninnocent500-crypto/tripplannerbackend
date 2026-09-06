import os
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/places", tags=["places"])

# Initialize Supabase client
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None


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
        # 1. Resolve destination_id from slug in destinations table
        dest_res = (
            supabase.table("destinations")
            .select("id")
            .eq("slug", slug)
            .execute()
        )

        if not dest_res.data:
            raise HTTPException(status_code=404, detail=f"Destination '{slug}' not found")

        destination_id = dest_res.data[0]["id"]

        # 2. Query destination_images table for accommodation images
        img_res = (
            supabase.table("destination_images")
            .select("image_url, caption")
            .eq("destination_id", destination_id)
            .eq("image_category", "accommodation")
            .limit(3)
            .execute()
        )

        if not img_res.data:
            raise HTTPException(status_code=404, detail=f"No accommodation images found for '{slug}'")

        # 3. Format into output DTO matching Android app expectations
        return [
            PlaceImageDto(
                url=row["image_url"],
                caption=row.get("caption"),
                order=idx,
            )
            for idx, row in enumerate(img_res.data)
            if row.get("image_url")
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching images for destination slug: %s", slug)
        raise HTTPException(status_code=500, detail=str(e))


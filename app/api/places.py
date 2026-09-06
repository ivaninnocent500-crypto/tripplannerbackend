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
        # Join destination_images with destinations table using slug directly
        res = (
            supabase.table("destination_images")
            .select("image_url, caption, destinations!inner(slug)")
            .eq("destinations.slug", slug)
            .limit(5)
            .execute()
        )

        # Fallback: If no images match slug join, fetch first available images
        rows = res.data if res.data else []
        if not rows:
            fallback_res = (
                supabase.table("destination_images")
                .select("image_url, caption")
                .limit(3)
                .execute()
            )
            rows = fallback_res.data if fallback_res.data else []

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


import os
import re
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client, Client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/places", tags=["places"])

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


# ============================================================================
# DTO — must mirror com.tta.africasafariguide.data.PlaceImageDto exactly.
#
# Kotlin side (Gson @SerializedName):
# id: String (non-null)
# image_url -> imageUrl: String (non-null)
# image_category -> imageCategory: String (non-null)
# caption: String? (nullable)
# display_order -> displayOrder: Int (non-null)
# width_px -> widthPx: Int? (nullable)
# height_px -> heightPx: Int? (nullable)
# lodge_id -> lodgeId: String? (nullable)
#
# by_alias=True on the response ensures FastAPI/Pydantic serializes using
# these snake_case keys instead of the Python field names.
# ============================================================================

class PlaceImageDto(BaseModel):
    id: str
    image_url: str = Field(alias="image_url")
    image_category: str = Field(alias="image_category")
    caption: Optional[str] = None
    display_order: int = Field(alias="display_order", default=0)
    width_px: Optional[int] = Field(alias="width_px", default=None)
    height_px: Optional[int] = Field(alias="height_px", default=None)
    lodge_id: Optional[str] = Field(alias="lodge_id", default=None)

    class Config:
        populate_by_name = True


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

        # 2. Fetch destination_images by destination_id.
        # Select every column the Kotlin PlaceImageDto needs — previously
        # this only selected image_url/caption, silently dropping id,
        # image_category, display_order, width_px, height_px, lodge_id.
        if dest_res.data:
            dest_id = dest_res.data[0]["id"]
            img_res = (
                supabase.table("destination_images")
                .select(
                    "id, image_url, image_category, caption, display_order, "
                    "width_px, height_px, lodge_id"
                )
                .eq("destination_id", dest_id)
                .order("display_order")
                .limit(5)
                .execute()
            )
            rows = img_res.data if img_res.data else []

        # 3. No same-destination match found — return an empty list rather
        # than an unrelated destination's photos. Returning arbitrary
        # images here would mislead the user into thinking they belong
        # to their chosen destination. The Kotlin ViewModel already
        # treats a 404/empty Tier-1 result as "fall back to Supabase",
        # so returning [] here is the correct, honest signal.
        if not rows:
            logger.info(
                "get_place_images(%s): no destination match, "
                "returning empty list instead of unrelated fallback images",
                slug,
            )
            return []

        # 4. Construct response DTO, matching every field the Kotlin
        # data class requires. Rows missing a hard-required field
        # (id, image_url, image_category) are skipped rather than
        # sent malformed, since Gson will fail to deserialize a
        # non-null field that's missing/null.
        results: List[PlaceImageDto] = []
        for row in rows:
            image_url = row.get("image_url")
            row_id = row.get("id")

            if not image_url or not row_id:
                logger.warning(
                    "get_place_images(%s): skipping row missing "
                    "id/image_url: %s",
                    slug,
                    row,
                )
                continue

            results.append(
                PlaceImageDto(
                    id=str(row_id),
                    image_url=image_url,
                    image_category=row.get("image_category") or "accommodation",
                    caption=row.get("caption"),
                    display_order=row.get("display_order") or 0,
                    width_px=row.get("width_px"),
                    height_px=row.get("height_px"),
                    lodge_id=row.get("lodge_id"),
                )
            )

        return results

    except Exception as e:
        logger.exception("Error fetching images for destination slug: %s", slug)
        raise HTTPException(status_code=500, detail=str(e))


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
    if not text:
        return ""
    text = text.lower().replace("_", "-").replace(" ", "-")
    return re.sub(r"[^a-z0-9\-]", "", text).strip("-")


@router.get("", response_model=dict)
@router.get("/", response_model=dict)
async def list_places(published_only: bool = False):
    """
    Returns {"places": [...]}, each place including hero_image_url —
    the image used by the WHERE-picker card shelf on
    ShapeYourJourneyScreen (DestinationCardGrid).

    hero_image_url priority for this endpoint specifically:
    display_order == 2 first (the "Shape your journey" card image,
    per current spec), falling back to display_order == 0, then to
    whatever the first available image is if neither exists. This is
    deliberately different from get_place_images() below, which
    returns the full ordered image list for a single destination
    (used by CabinetDayScreen's hero/day cards) rather than picking
    one image itself — that selection logic lives client-side in
    TripV2ViewModel/CabinetDayScreen using explicit display_order
    indices (0 for the day card, 1 for the journey header, per the
    latest spec).

    NOTE: this queries travel_places, not destinations. If your
    destination_images.destination_id foreign key was built against
    destinations.id rather than travel_places.id, this join will
    silently return no images for every place — confirm the two
    tables share the same id space (e.g. travel_places is a view
    over destinations, or destination_id in destination_images
    actually references travel_places) before relying on this in
    production.
    """
    if not supabase:
        return {"places": []}
    try:
        query = supabase.table("travel_places").select(
            "id, name, slug, destination_type, country, region, "
            "short_description, popularity_rank, "
            "destination_images(image_url, image_category, display_order)"
        )
        if published_only:
            query = query.eq("is_published", True)
        res = query.execute()
        rows = res.data if res.data else []

        places = []
        for row in rows:
            images = row.pop("destination_images", None) or []

            by_order = {img.get("display_order"): img for img in images if img.get("image_url")}

            chosen = (
                by_order.get(2)
                or by_order.get(0)
                or (sorted(images, key=lambda img: img.get("display_order") or 0)[:1] or [None])[0]
            )
            hero_image_url = chosen.get("image_url") if chosen else None

            row["hero_image_url"] = hero_image_url
            places.append(row)

        return {"places": places}
    except Exception as e:
        logger.warning(f"Error fetching places list: {e}")
        return {"places": []}


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
            supabase.table("travel_places")
            .select("id")
            .eq("slug", slug)
            .execute()
        )

        # 1b. Normalized slug lookup fallback
        if not dest_res.data and normalized_slug != slug:
            dest_res = (
                supabase.table("travel_places")
                .select("id")
                .eq("slug", normalized_slug)
                .execute()
            )

        # 1c. Partial match lookup fallback
        if not dest_res.data:
            clean_term = slug.replace("-", " ")
            dest_res = (
                supabase.table("travel_places")
                .select("id")
                .ilike("name", f"%{clean_term}%")
                .execute()
            )

        # 2. Fetch destination_images by destination_id, ordered so the
        # client can rely on index position matching display_order
        # (0, 1, 2...) for its own priority logic (day card vs journey
        # header vs WHERE-picker hero).
        if dest_res.data:
            dest_id = dest_res.data[0]["id"]
            img_res = (
                supabase.table("destination_images")
                .select(
                    "id, image_url, image_category, caption, display_order, "
                    "width_px, height_px, lodge_id"
                )
                .eq("destination_id", dest_id)
                .order("display_order", desc=False)
                .limit(5)
                .execute()
            )
            rows = img_res.data if img_res.data else []

        if not rows:
            logger.info(
                "get_place_images(%s): no destination match in travel_places",
                slug,
            )
            return []

        results: List[PlaceImageDto] = []
        for row in rows:
            image_url = row.get("image_url")
            row_id = row.get("id")

            if not image_url or not row_id:
                logger.warning(
                    "get_place_images(%s): skipping row missing id/image_url: %s",
                    slug,
                    row,
                )
                continue

            results.append(
                PlaceImageDto(
                    id=str(row_id),
                    image_url=image_url,
                    image_category=row.get("image_category") or "destination",
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



from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
import os
from supabase import create_client, Client

router = APIRouter(prefix="/api/places", tags=["places"])

# Initialize Supabase client inside backend
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class PlaceImageDto(BaseModel):
    url: str
    caption: Optional[str] = None
    order: int = 0

@router.get("/{slug}/images", response_model=List[PlaceImageDto])
async def get_place_images(slug: str):
    # 1. Resolve destination_id from slug
    dest_res = supabase.table("destinations").select("id").eq("slug", slug).execute()
    if not dest_res.data:
        raise HTTPException(status_code=404, detail=f"Destination '{slug}' not found")
        
    destination_id = dest_res.data[0]["id"]

    # 2. Query destination_images for accommodation images
    img_res = (
        supabase.table("destination_images")
        .select("image_url, caption")
        .eq("destination_id", destination_id)
        .eq("image_category", "accommodation")
        .execute()
    )

    if not img_res.data:
        raise HTTPException(status_code=404, detail=f"No accommodation images found for '{slug}'")

    # 3. Map to DTO output expected by Android app
    return [
        PlaceImageDto(
            url=item["image_url"],
            caption=item.get("caption"),
            order=idx
        )
        for idx, item in enumerate(img_res.data)
    ]


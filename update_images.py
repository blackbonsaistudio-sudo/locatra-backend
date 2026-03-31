"""
Update all places with real Wikipedia/Wikimedia Commons images.
Fetches the main image from each place's Wikipedia page.
"""
import asyncio
import httpx
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import urllib.parse
import time

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "test_database")

WIKI_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

async def get_wikipedia_images(place_name: str, client: httpx.AsyncClient) -> list:
    """Get images from Wikipedia page for a place."""
    images = []
    
    try:
        # Step 1: Search for the Wikipedia page
        search_params = {
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": place_name,
            "srlimit": 1,
        }
        resp = await client.get(WIKI_API, params=search_params, timeout=10)
        data = resp.json()
        
        if not data.get("query", {}).get("search"):
            return images
        
        title = data["query"]["search"][0]["title"]
        
        # Step 2: Get the page's main image (thumbnail)
        page_params = {
            "action": "query",
            "format": "json",
            "titles": title,
            "prop": "pageimages|images",
            "piprop": "original",
            "pilimit": 1,
            "imlimit": 10,
        }
        resp = await client.get(WIKI_API, params=page_params, timeout=10)
        data = resp.json()
        
        pages = data.get("query", {}).get("pages", {})
        for page_id, page in pages.items():
            # Get main image
            if "original" in page:
                img_url = page["original"]["source"]
                if is_valid_image(img_url):
                    images.append(img_url)
            
            # Get additional images from the page
            if "images" in page:
                for img in page["images"]:
                    img_title = img.get("title", "")
                    if not img_title.lower().endswith(('.svg', '.png')) or img_title.lower().endswith('.png'):
                        if any(skip in img_title.lower() for skip in ['icon', 'logo', 'flag', 'map', 'commons-logo', 'wiki', 'edit', 'symbol', 'pictogram', 'disambig', 'question_book', 'folder', 'ambox', 'padlock', 'red_pencil']):
                            continue
                        # Get the actual image URL from Wikimedia Commons
                        if len(images) < 5:
                            img_url = await get_commons_image_url(img_title, client)
                            if img_url and is_valid_image(img_url):
                                images.append(img_url)
        
    except Exception as e:
        pass
    
    return images[:5]  # Max 5 images

async def get_commons_image_url(file_title: str, client: httpx.AsyncClient) -> str:
    """Get the direct URL for a Wikimedia Commons file."""
    try:
        params = {
            "action": "query",
            "format": "json",
            "titles": file_title,
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": 800,
        }
        resp = await client.get(COMMONS_API, params=params, timeout=10)
        data = resp.json()
        
        pages = data.get("query", {}).get("pages", {})
        for page_id, page in pages.items():
            if "imageinfo" in page:
                info = page["imageinfo"][0]
                return info.get("thumburl") or info.get("url", "")
    except:
        pass
    return ""

def is_valid_image(url: str) -> bool:
    """Check if URL is a valid photo (not SVG, not too small, not an icon)."""
    url_lower = url.lower()
    if any(ext in url_lower for ext in ['.svg', '.gif']):
        return False
    if any(skip in url_lower for skip in ['icon', 'logo', 'flag_of', 'commons-logo', 'wikimedia', 'mediawiki', 'symbol', 'pictogram']):
        return False
    return True

async def update_places_images():
    import certifi
    mongo_kwargs = {}
    if 'mongodb+srv' in MONGO_URL or 'mongodb.net' in MONGO_URL:
        mongo_kwargs['tlsCAFile'] = certifi.where()
    
    client = AsyncIOMotorClient(MONGO_URL, **mongo_kwargs)
    db = client[DB_NAME]
    
    total = await db.places.count_documents({})
    print(f"Total places: {total}")
    
    places = await db.places.find({}, {"_id": 1, "name": 1, "category": 1, "images": 1}).to_list(length=2000)
    
    updated = 0
    failed = 0
    
    async with httpx.AsyncClient(
        headers={
            "User-Agent": "LocatraWorldExplorer/1.0 (https://locatra.app; blackbonsai.studio@gmail.com) httpx/0.28",
            "Accept": "application/json",
        },
        follow_redirects=True,
    ) as http_client:
        for i, place in enumerate(places):
            name = place["name"]
            
            try:
                images = await get_wikipedia_images(name, http_client)
                
                if images:
                    await db.places.update_one(
                        {"_id": place["_id"]},
                        {"$set": {
                            "images": images,
                            "image_url": images[0],
                        }}
                    )
                    updated += 1
                    img_count = len(images)
                    print(f"[{i+1}/{total}] ✅ {name}: {img_count} images")
                else:
                    failed += 1
                    print(f"[{i+1}/{total}] ❌ {name}: no images found")
                
                # Rate limit: 200 requests/sec max for Wikimedia API
                if (i + 1) % 5 == 0:
                    await asyncio.sleep(0.5)
                    
            except Exception as e:
                failed += 1
                print(f"[{i+1}/{total}] ❌ {name}: error - {str(e)[:50]}")
    
    print(f"\n=== DONE ===")
    print(f"Updated: {updated}")
    print(f"Failed: {failed}")
    print(f"Total: {total}")
    
    client.close()

if __name__ == "__main__":
    asyncio.run(update_places_images())

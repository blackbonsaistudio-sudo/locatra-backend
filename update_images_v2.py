"""
Update all places with real Wikipedia images.
"""
import asyncio
import httpx
import os
import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "test_database")

WIKI_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {
    "User-Agent": "LocatraWorldExplorer/1.0 (https://locatra.app; blackbonsai.studio@gmail.com) httpx/0.28",
    "Accept": "application/json",
}

SKIP_WORDS = ['icon', 'logo', 'flag_of', 'commons-logo', 'wikimedia', 'mediawiki', 
              'symbol', 'pictogram', 'disambig', 'question_book', 'folder', 'ambox',
              'padlock', 'red_pencil', 'edit-clear', 'text-x', 'increase', 'decrease',
              'crystal_clear', 'nuvola', 'gnome', 'tango', 'oxygen', 'fairytale']

def is_good_image(url_or_title: str) -> bool:
    lower = url_or_title.lower()
    if any(ext in lower for ext in ['.svg', '.gif', '.ogg', '.ogv', '.webm']):
        return False
    if any(w in lower for w in SKIP_WORDS):
        return False
    return True

async def get_images_for_place(name: str, client: httpx.AsyncClient) -> list:
    images = []
    try:
        # Search Wikipedia
        resp = await client.get(WIKI_API, params={
            "action": "query", "format": "json",
            "list": "search", "srsearch": name, "srlimit": 1,
        }, timeout=15)
        if resp.status_code != 200:
            return images
        data = resp.json()
        results = data.get("query", {}).get("search", [])
        if not results:
            return images
        title = results[0]["title"]
        
        # Get main page image
        resp = await client.get(WIKI_API, params={
            "action": "query", "format": "json", "titles": title,
            "prop": "pageimages|images", "piprop": "original",
            "pilimit": 1, "imlimit": 20,
        }, timeout=15)
        if resp.status_code != 200:
            return images
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        
        file_titles = []
        for pid, page in pages.items():
            if "original" in page:
                src = page["original"].get("source", "")
                if src and is_good_image(src):
                    images.append(src)
            if "images" in page:
                for img in page["images"]:
                    t = img.get("title", "")
                    if is_good_image(t):
                        file_titles.append(t)
        
        # Get URLs for additional images from commons
        for ft in file_titles[:8]:
            if len(images) >= 5:
                break
            try:
                resp = await client.get(COMMONS_API, params={
                    "action": "query", "format": "json", "titles": ft,
                    "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 800,
                }, timeout=10)
                if resp.status_code == 200:
                    cdata = resp.json()
                    cpages = cdata.get("query", {}).get("pages", {})
                    for cpid, cpage in cpages.items():
                        if "imageinfo" in cpage:
                            url = cpage["imageinfo"][0].get("thumburl") or cpage["imageinfo"][0].get("url", "")
                            if url and is_good_image(url) and url not in images:
                                images.append(url)
            except:
                continue
    except Exception as e:
        print(f"    ERROR: {e}")
    
    return images[:5]

async def main():
    mongo_kwargs = {}
    if 'mongodb+srv' in MONGO_URL or 'mongodb.net' in MONGO_URL:
        mongo_kwargs['tlsCAFile'] = certifi.where()
    
    mclient = AsyncIOMotorClient(MONGO_URL, **mongo_kwargs)
    db = mclient[DB_NAME]
    
    total = await db.places.count_documents({})
    print(f"Total places: {total}")
    
    places = await db.places.find({}, {"_id": 1, "name": 1}).to_list(length=2000)
    
    updated = 0
    failed = 0
    
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as hclient:
        for i, place in enumerate(places):
            name = place["name"]
            images = await get_images_for_place(name, hclient)
            
            if images:
                await db.places.update_one(
                    {"_id": place["_id"]},
                    {"$set": {"images": images, "image_url": images[0]}}
                )
                updated += 1
                print(f"[{i+1}/{total}] OK {name}: {len(images)} imgs")
            else:
                failed += 1
                print(f"[{i+1}/{total}] -- {name}: no imgs")
            
            # Rate limit
            if (i + 1) % 3 == 0:
                await asyncio.sleep(0.3)
    
    print(f"\nDONE: {updated} updated, {failed} failed, {total} total")
    mclient.close()

if __name__ == "__main__":
    asyncio.run(main())

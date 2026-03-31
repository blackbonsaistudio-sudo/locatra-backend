"""
Update all places with real Wikipedia images. V3 - fixed image filter bug.
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

# Only check FILENAMES, not full URLs
SKIP_FILENAMES = ['icon', 'logo', 'flag_of', 'commons-logo', 'symbol', 'pictogram',
                  'disambig', 'question_book', 'ambox', 'padlock', 'red_pencil',
                  'edit-clear', 'text-x', 'increase2', 'decrease2', 'crystal_clear',
                  'nuvola', 'gnome-', 'tango-', 'oxygen-', 'fairytale', 'wikidata',
                  'wikiquote', 'wikisource', 'wiktionary', 'wikivoyage', 'lock-',
                  'semi-protection', 'protection-shackle', 'portal-puzzle', 'folder_hexagonal']

def is_good_file(title_or_filename: str) -> bool:
    """Check if a file title (not URL) is a good photo."""
    lower = title_or_filename.lower()
    # Skip non-photo formats
    if any(ext in lower for ext in ['.svg', '.gif', '.ogg', '.ogv', '.webm', '.mid', '.flac']):
        return False
    # Skip utility/icon images by filename
    if any(w in lower for w in SKIP_FILENAMES):
        return False
    return True

async def get_imgs(name: str, hc: httpx.AsyncClient) -> list:
    imgs = []
    try:
        # Search Wikipedia
        resp = await hc.get(WIKI_API, params={
            "action": "query", "format": "json",
            "list": "search", "srsearch": name, "srlimit": 1,
        }, timeout=15)
        if resp.status_code != 200:
            return imgs
        data = resp.json()
        results = data.get("query", {}).get("search", [])
        if not results:
            return imgs
        title = results[0]["title"]

        # Get main image + page images list
        resp = await hc.get(WIKI_API, params={
            "action": "query", "format": "json", "titles": title,
            "prop": "pageimages|images", "piprop": "original",
            "pilimit": 1, "imlimit": 15,
        }, timeout=15)
        if resp.status_code != 200:
            return imgs
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})

        file_titles = []
        for pid, pg in pages.items():
            # Main image
            if "original" in pg:
                src = pg["original"].get("source", "")
                if src:
                    imgs.append(src)
            # Additional images
            if "images" in pg:
                for im in pg["images"]:
                    t = im.get("title", "")
                    if is_good_file(t):
                        file_titles.append(t)

        # Get URLs for additional images
        for ft in file_titles[:8]:
            if len(imgs) >= 5:
                break
            try:
                r = await hc.get(COMMONS_API, params={
                    "action": "query", "format": "json", "titles": ft,
                    "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 800,
                }, timeout=10)
                if r.status_code == 200:
                    cd = r.json()
                    for cpid, cp in cd.get("query", {}).get("pages", {}).items():
                        if "imageinfo" in cp:
                            url = cp["imageinfo"][0].get("thumburl") or cp["imageinfo"][0].get("url", "")
                            if url and url not in imgs:
                                imgs.append(url)
            except:
                continue
    except Exception as e:
        print(f"    ERR: {e}")

    return imgs[:5]

async def main():
    mongo_kwargs = {}
    if "mongodb.net" in MONGO_URL or "mongodb+srv" in MONGO_URL:
        mongo_kwargs["tlsCAFile"] = certifi.where()

    mc = AsyncIOMotorClient(MONGO_URL, **mongo_kwargs)
    db = mc[DB_NAME]

    total = await db.places.count_documents({})
    print(f"Total: {total}")

    places = await db.places.find({}, {"_id": 1, "name": 1}).to_list(length=2000)

    updated = 0
    failed = 0

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as hc:
        for i, p in enumerate(places):
            name = p["name"]
            imgs = await get_imgs(name, hc)

            if imgs:
                await db.places.update_one(
                    {"_id": p["_id"]},
                    {"$set": {"images": imgs, "image_url": imgs[0]}}
                )
                updated += 1
                print(f"[{i+1}/{total}] OK {name}: {len(imgs)} imgs")
            else:
                failed += 1
                print(f"[{i+1}/{total}] -- {name}")

            if (i + 1) % 3 == 0:
                await asyncio.sleep(0.3)

    print(f"\nDONE: {updated}/{total} updated, {failed} failed")
    mc.close()

if __name__ == "__main__":
    asyncio.run(main())

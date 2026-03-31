"""
Update places missing real Wikipedia images. Slower rate to avoid limits.
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

SKIP_FN = ['icon', 'logo', 'flag_of', 'commons-logo', 'symbol', 'pictogram',
           'disambig', 'question_book', 'ambox', 'padlock', 'red_pencil',
           'edit-clear', 'text-x', 'increase2', 'decrease2', 'crystal_clear',
           'nuvola', 'gnome-', 'tango-', 'oxygen-', 'fairytale', 'wikidata',
           'wikiquote', 'wikisource', 'wiktionary', 'wikivoyage', 'lock-',
           'semi-protection', 'protection-shackle', 'portal-puzzle', 'folder_hex']

def good_file(t):
    l = t.lower()
    if any(e in l for e in ['.svg','.gif','.ogg','.ogv','.webm','.mid','.flac']): return False
    if any(w in l for w in SKIP_FN): return False
    return True

async def get_imgs(name, hc):
    imgs = []
    try:
        r = await hc.get(WIKI_API, params={
            "action":"query","format":"json","list":"search","srsearch":name,"srlimit":1
        }, timeout=15)
        if r.status_code != 200: return imgs
        d = r.json()
        res = d.get("query",{}).get("search",[])
        if not res: return imgs
        title = res[0]["title"]

        r = await hc.get(WIKI_API, params={
            "action":"query","format":"json","titles":title,
            "prop":"pageimages|images","piprop":"original","pilimit":1,"imlimit":15
        }, timeout=15)
        if r.status_code != 200: return imgs
        d = r.json()
        fts = []
        for pid, pg in d.get("query",{}).get("pages",{}).items():
            if "original" in pg:
                src = pg["original"].get("source","")
                if src: imgs.append(src)
            if "images" in pg:
                for im in pg["images"]:
                    t = im.get("title","")
                    if good_file(t): fts.append(t)

        for ft in fts[:6]:
            if len(imgs) >= 3: break
            try:
                r = await hc.get(COMMONS_API, params={
                    "action":"query","format":"json","titles":ft,
                    "prop":"imageinfo","iiprop":"url","iiurlwidth":800
                }, timeout=10)
                if r.status_code == 200:
                    cd = r.json()
                    for cpid, cp in cd.get("query",{}).get("pages",{}).items():
                        if "imageinfo" in cp:
                            url = cp["imageinfo"][0].get("thumburl") or cp["imageinfo"][0].get("url","")
                            if url and url not in imgs: imgs.append(url)
            except: continue
    except Exception as e:
        pass
    return imgs[:5]

async def main():
    kw = {"tlsCAFile": certifi.where()} if "mongodb.net" in MONGO_URL else {}
    mc = AsyncIOMotorClient(MONGO_URL, **kw)
    db = mc[DB_NAME]

    # Only get places that still have unsplash images (not wikipedia)
    places = await db.places.find(
        {"image_url": {"$regex": "unsplash", "$options": "i"}},
        {"_id": 1, "name": 1}
    ).to_list(length=2000)
    
    total = len(places)
    print(f"Places needing real images: {total}")

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
                if (i+1) % 50 == 0 or i < 5:
                    print(f"[{i+1}/{total}] OK {name}: {len(imgs)} imgs")
            else:
                failed += 1
                if (i+1) % 100 == 0:
                    print(f"[{i+1}/{total}] -- {name}")

            # Slower rate: 1 request per second
            await asyncio.sleep(1.0)

    print(f"\nDONE: {updated}/{total} updated, {failed} failed")
    mc.close()

if __name__ == "__main__":
    asyncio.run(main())

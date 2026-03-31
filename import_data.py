"""
Import places data into production MongoDB Atlas.
Run: MONGO_URL="mongodb+srv://..." python3 import_data.py
"""
import asyncio
import json
from motor.motor_asyncio import AsyncIOMotorClient
import os

async def import_data():
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME", "locatra_prod")
    
    if not mongo_url:
        print("ERROR: Set MONGO_URL environment variable")
        print('Usage: MONGO_URL="mongodb+srv://..." DB_NAME="locatra_prod" python3 import_data.py')
        return
    
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    
    # Import places
    with open("places_export.json", "r") as f:
        places = json.load(f)
    
    if places:
        # Clear existing and insert fresh
        await db.places.delete_many({})
        await db.places.insert_many(places)
        print(f"Imported {len(places)} places")
    
    # Create indexes
    await db.places.create_index("place_id", unique=True)
    await db.places.create_index("category")
    await db.places.create_index("country_code")
    await db.places.create_index("status")
    await db.users.create_index("email", unique=True)
    await db.users.create_index("user_id", unique=True)
    print("Created database indexes")
    
    client.close()
    print("Done! Database is ready.")

if __name__ == "__main__":
    asyncio.run(import_data())

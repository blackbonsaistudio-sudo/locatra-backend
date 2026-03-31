"""
Export all places from local MongoDB to a JSON file for migration to production.
Run: python3 export_data.py
Output: places_export.json
"""
import asyncio
import json
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import os

load_dotenv()

async def export_data():
    client = AsyncIOMotorClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
    db = client[os.getenv("DB_NAME", "test_database")]
    
    # Export places
    places = await db.places.find({}, {"_id": 0}).to_list(1000)
    
    with open("places_export.json", "w") as f:
        json.dump(places, f, default=str, indent=2)
    
    print(f"Exported {len(places)} places to places_export.json")
    
    # Export users (without passwords)
    users = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(1000)
    
    with open("users_export.json", "w") as f:
        json.dump(users, f, default=str, indent=2)
    
    print(f"Exported {len(users)} users to users_export.json")
    
    client.close()

if __name__ == "__main__":
    asyncio.run(export_data())

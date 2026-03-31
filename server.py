from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, UploadFile, File, Form
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import jwt
import httpx
import base64
from seed_data import FREE_PLACES, PREMIUM_UNESCO_PLACES
from seed_beaches import BLUE_FLAG_BEACHES
from seed_hidden_gems import HIDDEN_GEMS

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
mongo_kwargs = {}
if 'mongodb+srv' in mongo_url or 'mongodb.net' in mongo_url:
    import certifi
    mongo_kwargs['tlsCAFile'] = certifi.where()
client = AsyncIOMotorClient(mongo_url, **mongo_kwargs)
db = client[os.environ['DB_NAME']]

# JWT Configuration
JWT_SECRET = os.environ.get('JWT_SECRET', 'your-super-secret-jwt-key-change-in-production-123456789')
JWT_ALGORITHM = "HS256"

# Admin emails (add your admin emails here)
ADMIN_EMAILS = ["admin@worldexplorer.com", "test@example.com"]

# Create the main app
app = FastAPI(title="World Explorer API")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===================== MODELS =====================

class UserBase(BaseModel):
    email: EmailStr
    name: str

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    user_id: str
    email: str
    name: str
    auth_type: str
    picture: Optional[str] = None
    favorites: List[str] = []
    is_admin: bool = False
    created_at: datetime

class Place(BaseModel):
    place_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    category: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    image_url: Optional[str] = None
    images: List[str] = []  # Array of image URLs (3-5 per place)
    photos: List[str] = []  # Base64 encoded photos
    rating: float = 0.0
    rating_count: int = 0
    tips: List[str] = []
    wikipedia_url: Optional[str] = None
    wikipedia_extract: Optional[str] = None
    is_user_submitted: bool = False
    submitted_by: Optional[str] = None
    submitted_by_name: Optional[str] = None
    status: str = "approved"
    rejection_reason: Optional[str] = None
    is_premium: bool = False
    is_unesco: bool = False
    # Category-specific fields
    best_season: Optional[str] = None
    difficulty: Optional[str] = None  # easy, moderate, hard
    crowd_level: Optional[str] = None  # low, medium, high
    water_quality: Optional[str] = None  # excellent, good, fair
    facilities: List[str] = []  # lifeguard, parking, showers, etc.
    era: Optional[str] = None  # for historic places
    terrain: Optional[str] = None  # for natural places
    continent: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PlaceCreate(BaseModel):
    name: str
    description: str
    category: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    image_url: Optional[str] = None
    tips: List[str] = []
    photo_base64: Optional[str] = None  # Base64 encoded photo

class Review(BaseModel):
    review_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    place_id: str
    user_id: str
    user_name: str
    user_picture: Optional[str] = None
    rating: int  # 1-5
    comment: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ReviewCreate(BaseModel):
    rating: int  # 1-5
    comment: str

class ModerationAction(BaseModel):
    action: str  # approve, reject
    reason: Optional[str] = None

class PhotoUpload(BaseModel):
    photo_base64: str

class SessionRequest(BaseModel):
    session_id: str

class GoogleAuthRequest(BaseModel):
    email: str
    name: str
    picture: Optional[str] = ""
    google_id: Optional[str] = ""

class WikipediaSearch(BaseModel):
    query: str

class SubscriptionRequest(BaseModel):
    plan: str = "monthly"  # currently only monthly

# ===================== AUTH HELPERS =====================

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        "type": "access"
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "type": "refresh"
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    session_token = request.cookies.get("session_token")
    
    if not token and not session_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    
    if session_token:
        session = await db.user_sessions.find_one(
            {"session_token": session_token},
            {"_id": 0}
        )
        if session:
            expires_at = session.get("expires_at")
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > datetime.now(timezone.utc):
                user = await db.users.find_one(
                    {"user_id": session["user_id"]},
                    {"_id": 0}
                )
                if user:
                    user["is_admin"] = user.get("email", "").lower() in [e.lower() for e in ADMIN_EMAILS]
                    return user
    
    if token:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            if payload.get("type") != "access":
                raise HTTPException(status_code=401, detail="Invalid token type")
            user = await db.users.find_one(
                {"user_id": payload["sub"]},
                {"_id": 0}
            )
            if not user:
                raise HTTPException(status_code=401, detail="User not found")
            user.pop("password_hash", None)
            user["is_admin"] = user.get("email", "").lower() in [e.lower() for e in ADMIN_EMAILS]
            return user
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")
    
    raise HTTPException(status_code=401, detail="Not authenticated")

async def get_optional_user(request: Request) -> Optional[dict]:
    try:
        return await get_current_user(request)
    except HTTPException:
        return None

async def require_admin(request: Request) -> dict:
    user = await get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# ===================== WIKIPEDIA HELPER =====================

async def fetch_wikipedia_data(place_name: str) -> dict:
    """Fetch Wikipedia extract and URL for a place"""
    try:
        headers = {
            "User-Agent": "WorldExplorerApp/1.0 (https://worldexplorer.app; contact@worldexplorer.app)"
        }
        async with httpx.AsyncClient(headers=headers) as client:
            search_url = "https://en.wikipedia.org/w/api.php"
            search_params = {
                "action": "query",
                "format": "json",
                "list": "search",
                "srsearch": place_name,
                "srlimit": 1
            }
            search_response = await client.get(search_url, params=search_params, timeout=10)
            search_data = search_response.json()
            
            if not search_data.get("query", {}).get("search"):
                return {"extract": None, "url": None}
            
            title = search_data["query"]["search"][0]["title"]
            
            extract_params = {
                "action": "query",
                "format": "json",
                "titles": title,
                "prop": "extracts|info",
                "exintro": True,
                "explaintext": True,
                "exsentences": 5,
                "inprop": "url"
            }
            extract_response = await client.get(search_url, params=extract_params, timeout=10)
            extract_data = extract_response.json()
            
            pages = extract_data.get("query", {}).get("pages", {})
            for page_id, page_data in pages.items():
                if page_id != "-1":
                    return {
                        "extract": page_data.get("extract", ""),
                        "url": page_data.get("fullurl", f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}")
                    }
            
            return {"extract": None, "url": None}
    except Exception as e:
        logger.error(f"Wikipedia fetch error: {e}")
        return {"extract": None, "url": None}

# ===================== AUTH ENDPOINTS =====================

@api_router.post("/auth/register")
async def register(user_data: UserCreate, response: Response):
    email = user_data.email.lower()
    
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    hashed_password = hash_password(user_data.password)
    
    user_doc = {
        "user_id": user_id,
        "email": email,
        "name": user_data.name,
        "password_hash": hashed_password,
        "auth_type": "email",
        "picture": None,
        "favorites": [],
        "is_admin": email.lower() in [e.lower() for e in ADMIN_EMAILS],
        "created_at": datetime.now(timezone.utc)
    }
    
    await db.users.insert_one(user_doc)
    
    access_token = create_access_token(user_id, email)
    refresh_token = create_refresh_token(user_id)
    
    response.set_cookie(
        key="access_token", value=access_token,
        httponly=True, secure=True, samesite="none",
        max_age=900, path="/"
    )
    response.set_cookie(
        key="refresh_token", value=refresh_token,
        httponly=True, secure=True, samesite="none",
        max_age=604800, path="/"
    )
    
    return {
        "user_id": user_id,
        "email": email,
        "name": user_data.name,
        "auth_type": "email",
        "favorites": [],
        "is_admin": user_doc["is_admin"],
        "created_at": user_doc["created_at"]
    }

@api_router.post("/auth/login")
async def login(user_data: UserLogin, response: Response):
    email = user_data.email.lower()
    
    user = await db.users.find_one({"email": email}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    if not user.get("password_hash"):
        raise HTTPException(status_code=401, detail="Please login with Google")
    
    if not verify_password(user_data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    access_token = create_access_token(user["user_id"], email)
    refresh_token = create_refresh_token(user["user_id"])
    
    response.set_cookie(
        key="access_token", value=access_token,
        httponly=True, secure=True, samesite="none",
        max_age=900, path="/"
    )
    response.set_cookie(
        key="refresh_token", value=refresh_token,
        httponly=True, secure=True, samesite="none",
        max_age=604800, path="/"
    )
    
    user.pop("password_hash", None)
    user["is_admin"] = email.lower() in [e.lower() for e in ADMIN_EMAILS]
    return user

@api_router.post("/auth/session")
async def process_google_session(session_req: SessionRequest, response: Response):
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
                headers={"X-Session-ID": session_req.session_id}
            )
            
            if res.status_code != 200:
                raise HTTPException(status_code=401, detail="Invalid session")
            
            session_data = res.json()
            email = session_data.get("email", "").lower()
            name = session_data.get("name", "")
            picture = session_data.get("picture", "")
            session_token = session_data.get("session_token", "")
            
            existing = await db.users.find_one({"email": email}, {"_id": 0})
            
            if existing:
                user_id = existing["user_id"]
                await db.users.update_one(
                    {"email": email},
                    {"$set": {"name": name, "picture": picture}}
                )
            else:
                user_id = f"user_{uuid.uuid4().hex[:12]}"
                user_doc = {
                    "user_id": user_id,
                    "email": email,
                    "name": name,
                    "auth_type": "google",
                    "picture": picture,
                    "favorites": [],
                    "is_admin": email.lower() in [e.lower() for e in ADMIN_EMAILS],
                    "created_at": datetime.now(timezone.utc)
                }
                await db.users.insert_one(user_doc)
            
            await db.user_sessions.insert_one({
                "user_id": user_id,
                "session_token": session_token,
                "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
                "created_at": datetime.now(timezone.utc)
            })
            
            response.set_cookie(
                key="session_token", value=session_token,
                httponly=True, secure=True, samesite="none",
                max_age=604800, path="/"
            )
            
            user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
            user.pop("password_hash", None)
            user["is_admin"] = email.lower() in [e.lower() for e in ADMIN_EMAILS]
            return user
            
    except httpx.RequestError as e:
        logger.error(f"Error processing Google session: {e}")
        raise HTTPException(status_code=500, detail="Authentication failed")

@api_router.post("/auth/google")
async def google_auth(google_req: GoogleAuthRequest, response: Response):
    """Native Google OAuth - accepts Google user info from expo-auth-session"""
    try:
        email = google_req.email.lower()
        name = google_req.name
        picture = google_req.picture or ""
        
        existing = await db.users.find_one({"email": email}, {"_id": 0})
        
        if existing:
            user_id = existing["user_id"]
            await db.users.update_one(
                {"email": email},
                {"$set": {"name": name, "picture": picture}}
            )
        else:
            user_id = f"user_{uuid.uuid4().hex[:12]}"
            user_doc = {
                "user_id": user_id,
                "email": email,
                "name": name,
                "auth_type": "google",
                "picture": picture,
                "favorites": [],
                "is_admin": email.lower() in [e.lower() for e in ADMIN_EMAILS],
                "created_at": datetime.now(timezone.utc)
            }
            await db.users.insert_one(user_doc)
        
        session_token = f"sess_{uuid.uuid4().hex}"
        await db.user_sessions.insert_one({
            "user_id": user_id,
            "session_token": session_token,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
            "created_at": datetime.now(timezone.utc)
        })
        
        response.set_cookie(
            key="session_token", value=session_token,
            httponly=True, secure=True, samesite="none",
            max_age=604800, path="/"
        )
        
        user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
        user.pop("password_hash", None)
        user["is_admin"] = email.lower() in [e.lower() for e in ADMIN_EMAILS]
        logger.info(f"Google auth successful for {email}")
        return user
        
    except Exception as e:
        logger.error(f"Error in Google auth: {e}")
        raise HTTPException(status_code=500, detail="Google authentication failed")


@api_router.get("/auth/me")
async def get_me(request: Request):
    user = await get_current_user(request)
    user.pop("password_hash", None)
    return user

@api_router.post("/auth/logout")
async def logout(request: Request, response: Response):
    session_token = request.cookies.get("session_token")
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    
    response.delete_cookie(key="access_token", path="/")
    response.delete_cookie(key="refresh_token", path="/")
    response.delete_cookie(key="session_token", path="/")
    
    return {"message": "Logged out successfully"}

@api_router.post("/auth/refresh")
async def refresh_token(request: Request, response: Response):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")
    
    try:
        payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        
        user = await db.users.find_one({"user_id": payload["sub"]}, {"_id": 0})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        
        access_token = create_access_token(user["user_id"], user["email"])
        
        response.set_cookie(
            key="access_token", value=access_token,
            httponly=True, secure=True, samesite="none",
            max_age=900, path="/"
        )
        
        return {"message": "Token refreshed"}
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ===================== PLACES ENDPOINTS =====================

@api_router.get("/places", response_model=List[Place])
async def get_places(
    request: Request,
    category: Optional[str] = None,
    country_code: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 2000,
    include_pending: bool = False
):
    user = await get_optional_user(request)

    query = {"status": "approved"} if not include_pending else {}
    
    # All places are available to all users (no premium gating)
    
    if category:
        query["category"] = category
    if country_code:
        query["country_code"] = country_code.upper()
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
            {"country": {"$regex": search, "$options": "i"}}
        ]
    
    places = await db.places.find(query, {"_id": 0}).limit(limit).to_list(limit)
    return places

@api_router.get("/places/{place_id}")
async def get_place(place_id: str):
    place = await db.places.find_one({"place_id": place_id}, {"_id": 0})
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    return place

@api_router.get("/places/country/{country_code}")
async def get_places_by_country(country_code: str):
    places = await db.places.find(
        {"country_code": country_code.upper(), "status": "approved"},
        {"_id": 0}
    ).to_list(100)
    return places

# ===================== USER SUBMITTED PLACES =====================

@api_router.post("/places/submit")
async def submit_place(place_data: PlaceCreate, request: Request):
    user = await get_current_user(request)
    
    wiki_data = await fetch_wikipedia_data(place_data.name)
    
    photos = []
    if place_data.photo_base64:
        photos.append(place_data.photo_base64)
    
    place_doc = {
        "place_id": str(uuid.uuid4()),
        "name": place_data.name,
        "description": place_data.description,
        "category": place_data.category,
        "country": place_data.country,
        "country_code": place_data.country_code.upper(),
        "latitude": place_data.latitude,
        "longitude": place_data.longitude,
        "image_url": place_data.image_url,
        "photos": photos,
        "rating": 0.0,
        "rating_count": 0,
        "tips": place_data.tips,
        "wikipedia_url": wiki_data.get("url"),
        "wikipedia_extract": wiki_data.get("extract"),
        "is_user_submitted": True,
        "submitted_by": user["user_id"],
        "submitted_by_name": user["name"],
        "status": "pending",  # Changed to pending for moderation
        "created_at": datetime.now(timezone.utc)
    }
    
    await db.places.insert_one(place_doc)
    
    return {
        "message": "Place submitted for review",
        "place_id": place_doc["place_id"],
        "status": "pending"
    }

@api_router.get("/places/user/submissions")
async def get_user_submissions(request: Request):
    user = await get_current_user(request)
    
    places = await db.places.find(
        {"submitted_by": user["user_id"]},
        {"_id": 0}
    ).to_list(100)
    
    return places

# ===================== MODERATION ENDPOINTS =====================

@api_router.get("/admin/pending")
async def get_pending_submissions(request: Request):
    """Get all pending place submissions (admin only)"""
    await require_admin(request)
    
    places = await db.places.find(
        {"status": "pending"},
        {"_id": 0}
    ).sort("created_at", -1).to_list(100)
    
    return places

@api_router.post("/admin/places/{place_id}/moderate")
async def moderate_place(place_id: str, action: ModerationAction, request: Request):
    """Approve or reject a place submission (admin only)"""
    admin = await require_admin(request)
    
    place = await db.places.find_one({"place_id": place_id})
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    
    if action.action == "approve":
        await db.places.update_one(
            {"place_id": place_id},
            {
                "$set": {
                    "status": "approved",
                    "moderated_by": admin["user_id"],
                    "moderated_at": datetime.now(timezone.utc)
                }
            }
        )
        return {"message": "Place approved", "status": "approved"}
    
    elif action.action == "reject":
        await db.places.update_one(
            {"place_id": place_id},
            {
                "$set": {
                    "status": "rejected",
                    "rejection_reason": action.reason,
                    "moderated_by": admin["user_id"],
                    "moderated_at": datetime.now(timezone.utc)
                }
            }
        )
        return {"message": "Place rejected", "status": "rejected"}
    
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

@api_router.get("/admin/stats")
async def get_admin_stats(request: Request):
    """Get moderation statistics (admin only)"""
    await require_admin(request)
    
    pending_count = await db.places.count_documents({"status": "pending"})
    approved_count = await db.places.count_documents({"status": "approved"})
    rejected_count = await db.places.count_documents({"status": "rejected"})
    total_reviews = await db.reviews.count_documents({})
    total_users = await db.users.count_documents({})
    
    return {
        "pending_submissions": pending_count,
        "approved_places": approved_count,
        "rejected_places": rejected_count,
        "total_reviews": total_reviews,
        "total_users": total_users
    }

# ===================== REVIEWS ENDPOINTS =====================

@api_router.get("/places/{place_id}/reviews")
async def get_reviews(place_id: str, limit: int = 50):
    """Get reviews for a place"""
    place = await db.places.find_one({"place_id": place_id})
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    
    reviews = await db.reviews.find(
        {"place_id": place_id},
        {"_id": 0}
    ).sort("created_at", -1).to_list(limit)
    
    return reviews

@api_router.post("/places/{place_id}/reviews")
async def add_review(place_id: str, review_data: ReviewCreate, request: Request):
    """Add a review to a place"""
    user = await get_current_user(request)
    
    place = await db.places.find_one({"place_id": place_id})
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    
    if review_data.rating < 1 or review_data.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")
    
    # Check if user already reviewed this place
    existing = await db.reviews.find_one({
        "place_id": place_id,
        "user_id": user["user_id"]
    })
    if existing:
        raise HTTPException(status_code=400, detail="You have already reviewed this place")
    
    review_doc = {
        "review_id": str(uuid.uuid4()),
        "place_id": place_id,
        "user_id": user["user_id"],
        "user_name": user["name"],
        "user_picture": user.get("picture"),
        "rating": review_data.rating,
        "comment": review_data.comment,
        "created_at": datetime.now(timezone.utc)
    }
    
    await db.reviews.insert_one(review_doc)
    
    # Update place rating
    all_reviews = await db.reviews.find({"place_id": place_id}, {"_id": 0, "rating": 1}).to_list(1000)
    avg_rating = sum(r["rating"] for r in all_reviews) / len(all_reviews)
    
    await db.places.update_one(
        {"place_id": place_id},
        {
            "$set": {
                "rating": round(avg_rating, 1),
                "rating_count": len(all_reviews)
            }
        }
    )
    
    return {
        "message": "Review added",
        "review_id": review_doc["review_id"],
        "new_rating": round(avg_rating, 1)
    }

@api_router.delete("/places/{place_id}/reviews/{review_id}")
async def delete_review(place_id: str, review_id: str, request: Request):
    """Delete a review (owner or admin only)"""
    user = await get_current_user(request)
    
    review = await db.reviews.find_one({"review_id": review_id, "place_id": place_id})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    
    if review["user_id"] != user["user_id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Not authorized to delete this review")
    
    await db.reviews.delete_one({"review_id": review_id})
    
    # Update place rating
    all_reviews = await db.reviews.find({"place_id": place_id}, {"_id": 0, "rating": 1}).to_list(1000)
    if all_reviews:
        avg_rating = sum(r["rating"] for r in all_reviews) / len(all_reviews)
    else:
        avg_rating = 0.0
    
    await db.places.update_one(
        {"place_id": place_id},
        {
            "$set": {
                "rating": round(avg_rating, 1),
                "rating_count": len(all_reviews)
            }
        }
    )
    
    return {"message": "Review deleted"}

# ===================== PHOTO ENDPOINTS =====================

@api_router.post("/places/{place_id}/photos")
async def upload_photo(place_id: str, photo_data: PhotoUpload, request: Request):
    """Upload a photo to a place (base64 encoded)"""
    user = await get_current_user(request)
    
    place = await db.places.find_one({"place_id": place_id})
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    
    # Validate base64
    try:
        # Check if it's a valid base64 string (with or without data URL prefix)
        photo_str = photo_data.photo_base64
        if photo_str.startswith("data:"):
            # Extract base64 part from data URL
            photo_str = photo_str.split(",")[1]
        base64.b64decode(photo_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image")
    
    # Add photo to place
    await db.places.update_one(
        {"place_id": place_id},
        {
            "$push": {"photos": photo_data.photo_base64},
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
    )
    
    # Track who uploaded
    await db.photo_uploads.insert_one({
        "photo_id": str(uuid.uuid4()),
        "place_id": place_id,
        "user_id": user["user_id"],
        "uploaded_at": datetime.now(timezone.utc)
    })
    
    return {"message": "Photo uploaded successfully"}

@api_router.get("/places/{place_id}/photos")
async def get_photos(place_id: str):
    """Get all photos for a place"""
    place = await db.places.find_one({"place_id": place_id}, {"_id": 0, "photos": 1})
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    
    return {"photos": place.get("photos", [])}

# ===================== WIKIPEDIA ENDPOINTS =====================

@api_router.post("/wikipedia/search")
async def search_wikipedia(search_data: WikipediaSearch):
    wiki_data = await fetch_wikipedia_data(search_data.query)
    return wiki_data

@api_router.get("/places/{place_id}/wikipedia")
async def get_place_wikipedia(place_id: str):
    place = await db.places.find_one({"place_id": place_id}, {"_id": 0})
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    
    wiki_data = await fetch_wikipedia_data(place["name"])
    
    if wiki_data.get("extract"):
        await db.places.update_one(
            {"place_id": place_id},
            {"$set": {
                "wikipedia_url": wiki_data.get("url"),
                "wikipedia_extract": wiki_data.get("extract")
            }}
        )
    
    return {
        "place_id": place_id,
        "name": place["name"],
        "wikipedia_url": wiki_data.get("url"),
        "wikipedia_extract": wiki_data.get("extract")
    }

# ===================== FAVORITES ENDPOINTS =====================

@api_router.get("/favorites")
async def get_favorites(request: Request):
    user = await get_current_user(request)
    favorites = user.get("favorites", [])
    
    if not favorites:
        return []
    
    places = await db.places.find(
        {"place_id": {"$in": favorites}},
        {"_id": 0}
    ).to_list(100)
    return places

@api_router.post("/favorites/{place_id}")
async def add_favorite(place_id: str, request: Request):
    user = await get_current_user(request)
    
    place = await db.places.find_one({"place_id": place_id})
    if not place:
        raise HTTPException(status_code=404, detail="Place not found")
    
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$addToSet": {"favorites": place_id}}
    )
    
    return {"message": "Added to favorites"}

@api_router.delete("/favorites/{place_id}")
async def remove_favorite(place_id: str, request: Request):
    user = await get_current_user(request)
    
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$pull": {"favorites": place_id}}
    )
    
    return {"message": "Removed from favorites"}

# ===================== OFFLINE SYNC ENDPOINTS =====================

@api_router.get("/sync/places")
async def sync_places(last_sync: Optional[str] = None):
    """Get places for offline sync"""
    query = {"status": "approved"}
    
    if last_sync:
        try:
            last_sync_dt = datetime.fromisoformat(last_sync)
            query["$or"] = [
                {"created_at": {"$gt": last_sync_dt}},
                {"updated_at": {"$gt": last_sync_dt}}
            ]
        except:
            pass
    
    places = await db.places.find(query, {"_id": 0}).to_list(500)
    
    return {
        "places": places,
        "sync_timestamp": datetime.now(timezone.utc).isoformat()
    }

@api_router.get("/sync/favorites")
async def sync_favorites(request: Request):
    """Get favorites for offline sync"""
    user = await get_current_user(request)
    favorites = user.get("favorites", [])
    
    if not favorites:
        return {"favorites": [], "sync_timestamp": datetime.now(timezone.utc).isoformat()}
    
    places = await db.places.find(
        {"place_id": {"$in": favorites}},
        {"_id": 0}
    ).to_list(100)
    
    return {
        "favorites": places,
        "sync_timestamp": datetime.now(timezone.utc).isoformat()
    }

# ===================== SUBSCRIPTION ENDPOINTS =====================

@api_router.get("/subscription/status")
async def get_subscription_status(request: Request):
    """Get current user's subscription status"""
    user = await get_current_user(request)
    
    sub = await db.subscriptions.find_one(
        {"user_id": user["user_id"], "status": "active"},
        {"_id": 0}
    )
    
    if sub:
        end_date = sub.get("end_date")
        if isinstance(end_date, str):
            end_date = datetime.fromisoformat(end_date)
        if end_date:
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            if end_date > datetime.now(timezone.utc):
                return {
                    "tier": "premium",
                    "end_date": end_date.isoformat(),
                    "plan": sub.get("plan", "monthly"),
                    "subscribed_at": sub.get("subscribed_at")
                }
            else:
                # Expired - mark as cancelled
                await db.subscriptions.update_one(
                    {"_id": sub.get("_id")},
                    {"$set": {"status": "expired"}}
                )
    
    return {
        "tier": "free",
        "end_date": None,
        "plan": None,
        "subscribed_at": None
    }

@api_router.post("/subscription/subscribe")
async def subscribe(sub_data: SubscriptionRequest, request: Request):
    """Subscribe to premium (mock payment - tap to subscribe)"""
    user = await get_current_user(request)
    
    # Check if already subscribed
    existing = await db.subscriptions.find_one(
        {"user_id": user["user_id"], "status": "active"}
    )
    if existing:
        end_date = existing.get("end_date")
        if isinstance(end_date, str):
            end_date = datetime.fromisoformat(end_date)
        if end_date:
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            if end_date > datetime.now(timezone.utc):
                return {
                    "success": True,
                    "message": "Already subscribed",
                    "tier": "premium",
                    "end_date": end_date.isoformat()
                }
    
    # Create subscription (mock payment - instant activation)
    now = datetime.now(timezone.utc)
    end_date = now + timedelta(days=30)  # 1 month
    
    sub_doc = {
        "subscription_id": str(uuid.uuid4()),
        "user_id": user["user_id"],
        "plan": sub_data.plan,
        "price": 4.99,
        "status": "active",
        "subscribed_at": now,
        "end_date": end_date,
        "payment_method": "mock",  # Would be replaced with real payment gateway
        "created_at": now
    }
    
    await db.subscriptions.insert_one(sub_doc)
    
    # Update user's premium flag
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"is_premium": True}}
    )
    
    logger.info(f"User {user['user_id']} subscribed to premium")
    
    return {
        "success": True,
        "message": "Welcome to Premium!",
        "tier": "premium",
        "end_date": end_date.isoformat()
    }

@api_router.post("/subscription/cancel")
async def cancel_subscription(request: Request):
    """Cancel premium subscription"""
    user = await get_current_user(request)
    
    result = await db.subscriptions.update_many(
        {"user_id": user["user_id"], "status": "active"},
        {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}}
    )
    
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"is_premium": False}}
    )
    
    if result.modified_count > 0:
        return {"message": "Subscription cancelled", "tier": "free"}
    else:
        return {"message": "No active subscription found", "tier": "free"}

@api_router.get("/places/premium/preview")
async def get_premium_preview():
    """Get a preview list of premium UNESCO places (titles only, for upselling)"""
    places = await db.places.find(
        {"is_premium": True, "status": "approved"},
        {"_id": 0, "place_id": 1, "name": 1, "country": 1, "category": 1, "image_url": 1, "is_premium": 1, "is_unesco": 1}
    ).to_list(100)
    return places

# ===================== SEED DATA =====================

async def seed_places():
    """Seed the database with 100+ free places and 30+ UNESCO premium places"""
    count = await db.places.count_documents({})
    
    # Check if data needs refresh - if any place is missing category_details fields, re-seed
    needs_reseed = False
    if count > 0:
        sample = await db.places.find_one({"category": "beach"})
        if sample and not sample.get("water_quality"):
            needs_reseed = True
            logger.info("Beach places missing category details, re-seeding...")
        if not needs_reseed:
            sample = await db.places.find_one({"category": "historic"})
            if sample and not sample.get("era"):
                needs_reseed = True
                logger.info("Historic places missing era details, re-seeding...")
    
    if needs_reseed:
        logger.info(f"Re-seeding {count} places with category-specific details...")
        await db.places.drop()
        count = 0
    
    if count > 0:
        logger.info(f"Database already has {count} places with category details")
        return
    
    now = datetime.now(timezone.utc)
    
    # Category-specific fields that should be extracted from seed data
    CATEGORY_FIELDS = {"tips", "is_premium", "is_unesco"}
    
    def make_place(p, base_rating, is_premium_default=False, is_unesco_default=False):
        """Build a place document from seed data, preserving all category-specific fields."""
        base_fields = {"name", "description", "category", "country", "country_code", 
                       "latitude", "longitude", "image_url"}
        extra_fields = {k: v for k, v in p.items() if k not in base_fields and k not in CATEGORY_FIELDS}
        return {
            "place_id": str(uuid.uuid4()),
            "photos": [],
            "rating": round(base_rating + (hash(p["name"]) % 10) / 10.0, 1),
            "rating_count": 0,
            "tips": p.get("tips", []),
            "wikipedia_url": None,
            "wikipedia_extract": None,
            "is_user_submitted": False,
            "submitted_by": None,
            "submitted_by_name": None,
            "status": "approved",
            "rejection_reason": None,
            "is_premium": p.get("is_premium", is_premium_default),
            "is_unesco": p.get("is_unesco", is_unesco_default),
            "best_season": p.get("best_season"),
            "difficulty": p.get("difficulty"),
            "crowd_level": p.get("crowd_level"),
            "water_quality": p.get("water_quality"),
            "facilities": p.get("facilities", []),
            "era": p.get("era"),
            "terrain": p.get("terrain"),
            "continent": p.get("continent"),
            "created_at": now,
            **{k: v for k, v in p.items() if k in base_fields}
        }

    all_places = []
    
    # Prepare free places
    for p in FREE_PLACES:
        all_places.append(make_place(p, 3.5, is_premium_default=False, is_unesco_default=False))
    
    # Prepare premium UNESCO places
    for p in PREMIUM_UNESCO_PLACES:
        all_places.append(make_place(p, 4.2, is_premium_default=True, is_unesco_default=True))
    
    # Prepare Blue Flag Beach places (premium)
    for p in BLUE_FLAG_BEACHES:
        all_places.append(make_place(p, 4.0, is_premium_default=True, is_unesco_default=False))
    
    # Prepare Hidden Gems places (premium)
    for p in HIDDEN_GEMS:
        all_places.append(make_place(p, 4.3, is_premium_default=True, is_unesco_default=False))
    
    await db.places.insert_many(all_places)
    free_count = len(FREE_PLACES)
    premium_count = len(PREMIUM_UNESCO_PLACES)
    beach_count = len(BLUE_FLAG_BEACHES)
    gem_count = len(HIDDEN_GEMS)
    logger.info(f"Seeded {free_count} free + {premium_count} UNESCO + {beach_count} beaches + {gem_count} gems = {free_count + premium_count + beach_count + gem_count} total")

# ===================== STARTUP =====================

@app.on_event("startup")
async def startup_event():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("user_id", unique=True)
    await db.places.create_index("place_id", unique=True)
    await db.places.create_index("category")
    await db.places.create_index("country_code")
    await db.places.create_index("status")
    await db.places.create_index("submitted_by")
    await db.places.create_index("is_premium")
    await db.places.create_index("is_unesco")
    await db.reviews.create_index("place_id")
    await db.reviews.create_index("user_id")
    await db.reviews.create_index([("place_id", 1), ("user_id", 1)], unique=True)
    await db.user_sessions.create_index("session_token")
    await db.user_sessions.create_index("user_id")
    await db.photo_uploads.create_index("place_id")
    await db.subscriptions.create_index("user_id")
    await db.subscriptions.create_index("status")
    
    await seed_places()
    
    logger.info("Application started successfully")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

# Include the router in the main app
# (moved to end of file - see below)

# ==========================================
# Analytics Endpoint
# ==========================================
@api_router.post("/analytics/event")
async def track_event(event: dict):
    """Lightweight analytics - track user events"""
    try:
        event_doc = {
            "event_type": event.get("type", "unknown"),
            "screen": event.get("screen"),
            "category": event.get("category"),
            "place_id": event.get("place_id"),
            "metadata": event.get("metadata", {}),
            "timestamp": datetime.utcnow(),
        }
        await db.analytics.insert_one(event_doc)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        return {"status": "ok"}  # Don't fail silently

@api_router.get("/analytics/summary")
async def analytics_summary(current_user: dict = Depends(get_current_user)):
    """Get analytics summary (admin only)"""
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    
    total_events = await db.analytics.count_documents({})
    screen_views = await db.analytics.count_documents({"event_type": "screen_view"})
    place_views = await db.analytics.count_documents({"event_type": "place_view"})
    premium_clicks = await db.analytics.count_documents({"event_type": "premium_click"})
    
    return {
        "total_events": total_events,
        "screen_views": screen_views,
        "place_views": place_views,
        "premium_clicks": premium_clicks,
    }


# Root endpoint
@api_router.get("/")
async def root():
    return {"message": "World Explorer API", "version": "3.0"}

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origin_regex=r".*",
    allow_methods=["*"],
    allow_headers=["*"],
)


from fastapi.responses import HTMLResponse

@app.get("/privacy-policy", response_class=HTMLResponse)
async def privacy_policy():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Locatra - Privacy Policy</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:800px;margin:0 auto;padding:20px;color:#333;line-height:1.6}
h1{color:#3498DB}h2{color:#2c3e50;margin-top:30px}p{margin:10px 0}
.updated{color:#666;font-style:italic}
</style>
</head>
<body>
<h1>Locatra - Privacy Policy</h1>
<p class="updated">Last updated: March 31, 2026</p>

<h2>1. Introduction</h2>
<p>Locatra ("we", "our", or "us") is a free world explorer app that helps you discover historic landmarks, museums, waterfalls, beaches, and natural places around the world. This Privacy Policy explains how we collect, use, and protect your information.</p>

<h2>2. Information We Collect</h2>
<p><strong>Account Information:</strong> When you create an account or sign in with Google, we collect your email address and display name.</p>
<p><strong>Usage Data:</strong> We collect anonymous usage data such as which places you view and your favorite places to improve the app experience.</p>
<p><strong>Location Data:</strong> With your permission, we may access your device location to show nearby places. This data is processed locally and not stored on our servers.</p>

<h2>3. How We Use Your Information</h2>
<p>We use your information to:</p>
<ul>
<li>Provide and maintain the app</li>
<li>Save your favorite places across devices</li>
<li>Show relevant advertisements through Google AdMob</li>
<li>Improve the app experience</li>
</ul>

<h2>4. Advertising</h2>
<p>We use Google AdMob to display advertisements. AdMob may collect device identifiers and usage data to serve personalized ads. You can opt out of personalized advertising in your device settings.</p>

<h2>5. Data Sharing</h2>
<p>We do not sell your personal information. We may share data with:</p>
<ul>
<li>Google (for authentication and advertising services)</li>
<li>MongoDB Atlas (for secure data storage)</li>
</ul>

<h2>6. Data Security</h2>
<p>We use industry-standard security measures including encrypted connections (HTTPS/TLS) and secure password hashing (bcrypt) to protect your data.</p>

<h2>7. Data Retention and Deletion</h2>
<p>You can delete your account and associated data at any time by contacting us at blackbonsai.studio@gmail.com.</p>

<h2>8. Children's Privacy</h2>
<p>Locatra is not directed at children under 13. We do not knowingly collect personal information from children under 13.</p>

<h2>9. Changes to This Policy</h2>
<p>We may update this Privacy Policy from time to time. We will notify you of any changes by posting the new policy in the app.</p>

<h2>10. Contact Us</h2>
<p>If you have questions about this Privacy Policy, contact us at:<br>
<strong>Email:</strong> blackbonsai.studio@gmail.com</p>
</body>
</html>"""

@app.get("/terms", response_class=HTMLResponse)
async def terms_of_service():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Locatra - Terms of Service</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:800px;margin:0 auto;padding:20px;color:#333;line-height:1.6}
h1{color:#3498DB}h2{color:#2c3e50;margin-top:30px}p{margin:10px 0}
.updated{color:#666;font-style:italic}
</style>
</head>
<body>
<h1>Locatra - Terms of Service</h1>
<p class="updated">Last updated: March 31, 2026</p>

<h2>1. Acceptance of Terms</h2>
<p>By downloading or using Locatra, you agree to be bound by these Terms of Service.</p>

<h2>2. Description of Service</h2>
<p>Locatra is a free mobile application that allows users to explore world landmarks, museums, waterfalls, beaches, and natural places on an interactive map. The service is provided free of charge and supported by advertisements.</p>

<h2>3. User Accounts</h2>
<p>You may create an account using email/password or Google Sign-In. You are responsible for maintaining the security of your account credentials.</p>

<h2>4. Acceptable Use</h2>
<p>You agree not to misuse the service, including but not limited to: submitting false or misleading place information, attempting to circumvent security measures, or using the service for any illegal purpose.</p>

<h2>5. Content</h2>
<p>Place descriptions and images are sourced from Wikipedia and Wikimedia Commons under their respective licenses. User-submitted content must be accurate and not infringe on any third-party rights.</p>

<h2>6. Advertisements</h2>
<p>Locatra displays advertisements through Google AdMob. These ads help keep the app free for all users.</p>

<h2>7. Disclaimer</h2>
<p>Locatra is provided "as is" without warranties of any kind. We do not guarantee the accuracy of place information, travel conditions, or safety at any listed location. Always verify travel information independently.</p>

<h2>8. Limitation of Liability</h2>
<p>We shall not be liable for any indirect, incidental, or consequential damages arising from your use of the app.</p>

<h2>9. Changes to Terms</h2>
<p>We reserve the right to modify these terms at any time. Continued use of the app constitutes acceptance of modified terms.</p>

<h2>10. Contact</h2>
<p>For questions about these terms, contact us at:<br>
<strong>Email:</strong> blackbonsai.studio@gmail.com</p>
</body>
</html>"""


# Include the router (MUST be after all routes are defined)
app.include_router(api_router)

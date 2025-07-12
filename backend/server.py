from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, File, UploadFile
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
import uuid
from datetime import datetime, timedelta
import bcrypt
import jwt
import base64

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI(title="ReWear - Community Clothing Exchange API")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Security
security = HTTPBearer()
SECRET_KEY = "rewear_secret_key_2025"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24 * 7  # 7 days

# Models
class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: EmailStr
    name: str
    password_hash: str
    points: int = Field(default=100)  # Starting points for new users
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_admin: bool = Field(default=False)

class UserCreate(BaseModel):
    email: EmailStr
    name: str
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    points: int
    is_admin: bool

class Item(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str
    category: str  # tops, bottoms, shoes, accessories, etc.
    type: str  # shirt, jeans, sneakers, etc.
    size: str
    condition: str  # new, like-new, good, fair
    tags: List[str] = Field(default_factory=list)
    images: List[str] = Field(default_factory=list)  # base64 encoded images
    owner_id: str
    owner_name: str
    points_value: int = Field(default=10)
    status: str = Field(default="available")  # available, pending, swapped
    created_at: datetime = Field(default_factory=datetime.utcnow)
    approved: bool = Field(default=True)  # For admin moderation

class ItemCreate(BaseModel):
    title: str
    description: str
    category: str
    type: str
    size: str
    condition: str
    tags: List[str] = Field(default_factory=list)
    images: List[str] = Field(default_factory=list)
    points_value: int = Field(default=10)

class SwapRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    item_id: str
    requester_id: str
    requester_name: str
    owner_id: str
    swap_type: str  # "direct" or "points"
    offered_item_id: Optional[str] = None  # For direct swaps
    message: str = ""
    status: str = Field(default="pending")  # pending, accepted, rejected, completed
    created_at: datetime = Field(default_factory=datetime.utcnow)

class SwapRequestCreate(BaseModel):
    item_id: str
    swap_type: str
    offered_item_id: Optional[str] = None
    message: str = ""

# Helper functions
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    
    user = await db.users.find_one({"id": user_id})
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return UserResponse(**user)

# Authentication endpoints
@api_router.post("/auth/register", response_model=dict)
async def register(user_data: UserCreate):
    # Check if user already exists
    existing_user = await db.users.find_one({"email": user_data.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Hash password and create user
    hashed_password = hash_password(user_data.password)
    user = User(
        email=user_data.email,
        name=user_data.name,
        password_hash=hashed_password
    )
    
    await db.users.insert_one(user.dict())
    
    # Create access token
    access_token = create_access_token(data={"sub": user.id})
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": UserResponse(**user.dict())
    }

@api_router.post("/auth/login", response_model=dict)
async def login(user_data: UserLogin):
    user = await db.users.find_one({"email": user_data.email})
    if not user or not verify_password(user_data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    
    access_token = create_access_token(data={"sub": user["id"]})
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": UserResponse(**user)
    }

@api_router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: UserResponse = Depends(get_current_user)):
    return current_user

# Item endpoints
@api_router.post("/items", response_model=Item)
async def create_item(item_data: ItemCreate, current_user: UserResponse = Depends(get_current_user)):
    item = Item(
        **item_data.dict(),
        owner_id=current_user.id,
        owner_name=current_user.name
    )
    
    await db.items.insert_one(item.dict())
    return item

@api_router.get("/items", response_model=List[Item])
async def get_items(category: Optional[str] = None, limit: int = 20, skip: int = 0):
    query = {"status": "available", "approved": True}
    if category:
        query["category"] = category
    
    items = await db.items.find(query).skip(skip).limit(limit).to_list(limit)
    return [Item(**item) for item in items]

@api_router.get("/items/featured", response_model=List[Item])
async def get_featured_items():
    # Get 6 most recent items for featured carousel
    items = await db.items.find({"status": "available", "approved": True}).sort("created_at", -1).limit(6).to_list(6)
    return [Item(**item) for item in items]

@api_router.get("/items/{item_id}", response_model=Item)
async def get_item(item_id: str):
    item = await db.items.find_one({"id": item_id})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return Item(**item)

@api_router.get("/items/user/{user_id}", response_model=List[Item])
async def get_user_items(user_id: str):
    items = await db.items.find({"owner_id": user_id}).to_list(100)
    return [Item(**item) for item in items]

# Swap endpoints
@api_router.post("/swaps", response_model=SwapRequest)
async def create_swap_request(swap_data: SwapRequestCreate, current_user: UserResponse = Depends(get_current_user)):
    # Get the item details
    item = await db.items.find_one({"id": swap_data.item_id})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    if item["owner_id"] == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot swap your own item")
    
    # For points-based swaps, check if user has enough points
    if swap_data.swap_type == "points":
        if current_user.points < item["points_value"]:
            raise HTTPException(status_code=400, detail="Insufficient points")
    
    swap_request = SwapRequest(
        item_id=swap_data.item_id,
        requester_id=current_user.id,
        requester_name=current_user.name,
        owner_id=item["owner_id"],
        swap_type=swap_data.swap_type,
        offered_item_id=swap_data.offered_item_id,
        message=swap_data.message
    )
    
    await db.swap_requests.insert_one(swap_request.dict())
    return swap_request

@api_router.get("/swaps/incoming", response_model=List[SwapRequest])
async def get_incoming_swaps(current_user: UserResponse = Depends(get_current_user)):
    swaps = await db.swap_requests.find({"owner_id": current_user.id}).to_list(100)
    return [SwapRequest(**swap) for swap in swaps]

@api_router.get("/swaps/outgoing", response_model=List[SwapRequest])
async def get_outgoing_swaps(current_user: UserResponse = Depends(get_current_user)):
    swaps = await db.swap_requests.find({"requester_id": current_user.id}).to_list(100)
    return [SwapRequest(**swap) for swap in swaps]

@api_router.put("/swaps/{swap_id}/accept")
async def accept_swap(swap_id: str, current_user: UserResponse = Depends(get_current_user)):
    swap = await db.swap_requests.find_one({"id": swap_id, "owner_id": current_user.id})
    if not swap:
        raise HTTPException(status_code=404, detail="Swap request not found")
    
    # Update swap status
    await db.swap_requests.update_one(
        {"id": swap_id},
        {"$set": {"status": "accepted"}}
    )
    
    # Handle points transfer for points-based swaps
    if swap["swap_type"] == "points":
        # Get item details
        item = await db.items.find_one({"id": swap["item_id"]})
        
        # Deduct points from requester and add to owner
        await db.users.update_one(
            {"id": swap["requester_id"]},
            {"$inc": {"points": -item["points_value"]}}
        )
        await db.users.update_one(
            {"id": current_user.id},
            {"$inc": {"points": item["points_value"]}}
        )
        
        # Mark item as swapped
        await db.items.update_one(
            {"id": swap["item_id"]},
            {"$set": {"status": "swapped"}}
        )
    
    return {"message": "Swap request accepted"}

@api_router.put("/swaps/{swap_id}/reject")
async def reject_swap(swap_id: str, current_user: UserResponse = Depends(get_current_user)):
    await db.swap_requests.update_one(
        {"id": swap_id, "owner_id": current_user.id},
        {"$set": {"status": "rejected"}}
    )
    return {"message": "Swap request rejected"}

# Admin endpoints
@api_router.get("/admin/items", response_model=List[Item])
async def get_admin_items(current_user: UserResponse = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    items = await db.items.find().to_list(1000)
    return [Item(**item) for item in items]

@api_router.put("/admin/items/{item_id}/approve")
async def approve_item(item_id: str, current_user: UserResponse = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    await db.items.update_one(
        {"id": item_id},
        {"$set": {"approved": True}}
    )
    return {"message": "Item approved"}

@api_router.delete("/admin/items/{item_id}")
async def delete_item(item_id: str, current_user: UserResponse = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    await db.items.delete_one({"id": item_id})
    return {"message": "Item deleted"}

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
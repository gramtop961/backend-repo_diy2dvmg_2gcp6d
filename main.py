import os
import string
import random
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import datetime, timezone

from database import db, create_document, get_documents

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Models
# -----------------------------
class CreateRoomRequest(BaseModel):
    name: str
    avatar: Optional[str] = None

class JoinRoomRequest(BaseModel):
    name: str
    avatar: Optional[str] = None
    code: str

class StartGameRequest(BaseModel):
    code: str

# -----------------------------
# Helpers
# -----------------------------

def gen_code(length: int = 6) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------
# Database operations
# -----------------------------
ROOMS_COLLECTION = "gameroom"
PLAYERS_COLLECTION = "player"


def get_room_by_code(code: str) -> Optional[Dict[str, Any]]:
    rooms = get_documents(ROOMS_COLLECTION, {"code": code}, limit=1)
    return rooms[0] if rooms else None


# -----------------------------
# WebSocket connection manager
# -----------------------------
class ConnectionManager:
    def __init__(self):
        # room_code -> set of websockets
        self.active: Dict[str, List[WebSocket]] = {}

    async def connect(self, room: str, websocket: WebSocket):
        await websocket.accept()
        self.active.setdefault(room, []).append(websocket)

    def disconnect(self, room: str, websocket: WebSocket):
        if room in self.active and websocket in self.active[room]:
            self.active[room].remove(websocket)
            if not self.active[room]:
                del self.active[room]

    async def broadcast(self, room: str, message: Dict[str, Any]):
        if room not in self.active:
            return
        living = []
        for ws in self.active[room]:
            try:
                await ws.send_json(message)
                living.append(ws)
            except Exception:
                pass
        self.active[room] = living


manager = ConnectionManager()


# -----------------------------
# Basic endpoints
# -----------------------------
@app.get("/")
def read_root():
    return {"message": "Rider Online backend running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    return response


# -----------------------------
# Room management endpoints
# -----------------------------
@app.post("/rooms/create")
def create_room(payload: CreateRoomRequest):
    # create player
    player_id = create_document(
        PLAYERS_COLLECTION,
        {
            "name": payload.name,
            "avatar": payload.avatar,
            "created_at": now_iso(),
        },
    )
    # generate unique code
    tries = 0
    code = gen_code()
    while get_room_by_code(code) is not None and tries < 5:
        code = gen_code()
        tries += 1

    room_doc = {
        "code": code,
        "host_id": player_id,
        "status": "waiting",
        "players": [player_id],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    create_document(ROOMS_COLLECTION, room_doc)
    return {"code": code, "player_id": player_id, "status": "waiting"}


@app.post("/rooms/join")
def join_room(payload: JoinRoomRequest):
    room = get_room_by_code(payload.code.upper())
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.get("status") == "active":
        raise HTTPException(status_code=400, detail="Game already started")

    player_id = create_document(
        PLAYERS_COLLECTION,
        {
            "name": payload.name,
            "avatar": payload.avatar,
            "created_at": now_iso(),
        },
    )

    # push player to room
    db[ROOMS_COLLECTION].update_one({"code": room["code"]}, {"$addToSet": {"players": player_id}, "$set": {"updated_at": now_iso()}})

    return {"code": room["code"], "player_id": player_id}


@app.get("/rooms/{code}")
def get_room(code: str):
    room = get_room_by_code(code.upper())
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    # fetch player names for convenience
    players = list(db[PLAYERS_COLLECTION].find({"_id": {"$in": [db[PLAYERS_COLLECTION].name and __import__('bson').ObjectId(pid) if len(pid)==24 else None for pid in room.get('players', [])]}}))
    # Above is potentially messy due to ObjectId; return just IDs
    return {
        "code": room["code"],
        "host_id": room["host_id"],
        "status": room.get("status", "waiting"),
        "players": room.get("players", []),
    }


@app.post("/rooms/start")
def start_game(payload: StartGameRequest):
    room = get_room_by_code(payload.code.upper())
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    db[ROOMS_COLLECTION].update_one({"code": room["code"]}, {"$set": {"status": "active", "updated_at": now_iso()}})
    return {"ok": True}


# -----------------------------
# WebSocket for real-time sync
# -----------------------------
@app.websocket("/ws/rooms/{code}")
async def ws_room(websocket: WebSocket, code: str):
    code = code.upper()
    await manager.connect(code, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # Expected payload: {"type": "state"|"chat", "player_id": "...", "payload": {...}}
            await manager.broadcast(code, data)
    except WebSocketDisconnect:
        manager.disconnect(code, websocket)
    except Exception:
        manager.disconnect(code, websocket)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

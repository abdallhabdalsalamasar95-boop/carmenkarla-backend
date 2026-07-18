import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

_FIREBASE_IMPORT_ERROR = ""

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception as _ex:  # pragma: no cover - optional dependency at runtime
    firebase_admin = None
    credentials = None
    firestore = None
    _FIREBASE_IMPORT_ERROR = str(_ex)

ROOT = Path(__file__).resolve().parent

load_dotenv(ROOT / ".env")

_STORAGE_ROOT_ENV = (os.getenv("STORAGE_ROOT", "") or "").strip()
STORAGE_ROOT = Path(_STORAGE_ROOT_ENV) if _STORAGE_ROOT_ENV else ROOT
if not STORAGE_ROOT.is_absolute():
    STORAGE_ROOT = (ROOT / STORAGE_ROOT).resolve()

DATA_DIR = STORAGE_ROOT / "data"
UPLOAD_DIR = STORAGE_ROOT / "uploads"
PRODUCTS_FILE = DATA_DIR / "products.json"
NOTIFICATIONS_FILE = DATA_DIR / "notifications.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = int((os.getenv("PORT", "8080") or "8080").strip())
API_TOKEN = (os.getenv("API_TOKEN", "") or "").strip()
CORS_ORIGIN = (os.getenv("CORS_ORIGIN", "") or "").strip()
# Explicit public base URL used for image links (so phone can access them via LAN IP).
# If not set, auto-detected from SERVER_HOST or machine's LAN IP.
_SERVER_BASE_URL_ENV = (os.getenv("SERVER_BASE_URL", "") or "").strip().rstrip("/")
_FIREBASE_SERVICE_ACCOUNT_FILE = (os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "") or "").strip()
_FIREBASE_SERVICE_ACCOUNT_JSON = (os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "") or "").strip()
_FIREBASE_PROJECT_ID = (os.getenv("FIREBASE_PROJECT_ID", "") or "").strip()

_FIRESTORE_DB = None
_FIREBASE_INIT_ERROR = ""


def _resolve_public_base() -> str:
    """Return the base URL the phone should use to reach this server."""
    if _SERVER_BASE_URL_ENV:
        return _SERVER_BASE_URL_ENV
    # Try to find the LAN IP automatically (skip loopback/link-local).
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
        return f"http://{lan_ip}:{PORT}"
    except Exception:
        return f"http://127.0.0.1:{PORT}"


def _request_public_base() -> str:
    host_url = (request.host_url or "").strip().rstrip("/")
    if host_url and not re.search(r"://(?:127\.0\.0\.1|localhost)(?::|/|$)", host_url, re.I):
        return host_url
    return PUBLIC_BASE


def _init_firestore() -> None:
    global _FIRESTORE_DB, _FIREBASE_INIT_ERROR

    if _FIRESTORE_DB is not None:
        return

    if firebase_admin is None or firestore is None:
        _FIREBASE_INIT_ERROR = (
            "firebase dependencies import failed"
            + (f": {_FIREBASE_IMPORT_ERROR}" if _FIREBASE_IMPORT_ERROR else "")
        )
        return

    try:
        app_obj = firebase_admin.get_app() if firebase_admin._apps else None
    except Exception:
        app_obj = None

    try:
        if app_obj is None:
            if _FIREBASE_SERVICE_ACCOUNT_FILE:
                service_path = Path(_FIREBASE_SERVICE_ACCOUNT_FILE)
                if not service_path.is_absolute():
                    service_path = (ROOT / service_path).resolve()
                if not service_path.exists():
                    _FIREBASE_INIT_ERROR = f"Service account file not found: {service_path}"
                    return
                cred = credentials.Certificate(str(service_path))
                app_obj = firebase_admin.initialize_app(cred)
            elif _FIREBASE_SERVICE_ACCOUNT_JSON:
                service_json = json.loads(_FIREBASE_SERVICE_ACCOUNT_JSON)
                cred = credentials.Certificate(service_json)
                app_obj = firebase_admin.initialize_app(cred)
            elif _FIREBASE_PROJECT_ID:
                app_obj = firebase_admin.initialize_app(options={"projectId": _FIREBASE_PROJECT_ID})
            else:
                _FIREBASE_INIT_ERROR = (
                    "Missing Firebase credentials. Configure FIREBASE_SERVICE_ACCOUNT_FILE "
                    "or FIREBASE_SERVICE_ACCOUNT_JSON"
                )
                return

        _FIRESTORE_DB = firestore.client(app_obj)
        _FIREBASE_INIT_ERROR = ""
    except Exception as ex:
        _FIRESTORE_DB = None
        _FIREBASE_INIT_ERROR = str(ex)


def _firestore_db() -> tuple[Optional[Any], str]:
    if _FIRESTORE_DB is not None:
        return _FIRESTORE_DB, ""
    _init_firestore()
    if _FIRESTORE_DB is not None:
        return _FIRESTORE_DB, ""
    return None, (_FIREBASE_INIT_ERROR or "Failed to initialize Firestore")

PUBLIC_BASE = _resolve_public_base()

if not PRODUCTS_FILE.exists():
    PRODUCTS_FILE.write_text("[]", encoding="utf-8")

if not NOTIFICATIONS_FILE.exists():
    NOTIFICATIONS_FILE.write_text("[]", encoding="utf-8")

app = Flask(__name__)

if CORS_ORIGIN:
    CORS(app, resources={r"/*": {"origins": [CORS_ORIGIN]}})
else:
    CORS(app)


def read_products() -> List[Dict[str, Any]]:
    try:
        raw = PRODUCTS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:
        return []


def write_products(products: List[Dict[str, Any]]) -> None:
    PRODUCTS_FILE.write_text(
        json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_notifications() -> List[Dict[str, Any]]:
    try:
        raw = NOTIFICATIONS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:
        return []


def write_notifications(items: List[Dict[str, Any]]) -> None:
    NOTIFICATIONS_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def normalize_notification_item(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = current or {}
    now_ms = int(time.time() * 1000)

    nid = str(payload.get("id") or cur.get("id") or f"n_srv_{now_ms}_{uuid.uuid4().hex[:8]}").strip()
    title = str(payload.get("title") or cur.get("title") or "").strip()
    body = str(payload.get("body") or cur.get("body") or "").strip()
    target = str(payload.get("target") or cur.get("target") or "").strip()
    target_id = str(payload.get("targetId") or cur.get("targetId") or "").strip()
    audience = str(payload.get("audience") or cur.get("audience") or "all").strip().lower()
    uid = str(payload.get("uid") or cur.get("uid") or "").strip()

    if audience not in {"all", "user"}:
        audience = "all"

    return {
        "id": nid,
        "title": title,
        "body": body,
        "target": target,
        "targetId": target_id,
        "audience": audience,
        "uid": uid,
        "createdAtMs": as_int(payload.get("createdAtMs", cur.get("createdAtMs", now_ms)), now_ms),
    }


def as_number(v: Any, fallback: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return fallback


def as_int(v: Any, fallback: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return fallback


def as_hidden_int(v: Any) -> int:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if int(v) != 0 else 0
    s = str(v or "").strip().lower()
    return 1 if s in {"1", "true", "yes", "on"} else 0


def normalize_image_urls(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]

    if value is None:
        return []

    s = str(value).strip()
    if not s:
        return []

    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass

    return [x.strip() for x in s.split(",") if x.strip()]


def normalize_product(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = current or {}
    now = int(time.time() * 1000)

    pid = str(payload.get("id") or cur.get("id") or uuid.uuid4()).strip()
    name = str(payload.get("name") or cur.get("name") or "").strip()
    description = str(payload.get("description") or cur.get("description") or "").strip()
    category = str(payload.get("category") or cur.get("category") or "غير مصنف").strip()
    tags = str(payload.get("tags") or cur.get("tags") or "").strip()

    image_url = str(payload.get("imageUrl") or cur.get("imageUrl") or "").strip()
    image_urls = normalize_image_urls(payload.get("imageUrls", cur.get("imageUrls", [])))

    if not image_url and image_urls:
        image_url = image_urls[0]

    product = {
        "id": pid,
        "name": name,
        "price": as_number(payload.get("price", cur.get("price", 0))),
        "oldPrice": as_number(payload.get("oldPrice", cur.get("oldPrice", 0))),
        "imageUrl": image_url,
        "imageUrls": image_urls,
        "description": description,
        "category": category,
        "tags": tags,
        "rating": as_number(payload.get("rating", cur.get("rating", 0))),
        "reviewsCount": max(0, as_int(payload.get("reviewsCount", cur.get("reviewsCount", 0)))),
        "isHidden": as_hidden_int(payload.get("isHidden", cur.get("isHidden", 0))),
        "sizes": str(payload.get("sizes") if payload.get("sizes") is not None else cur.get("sizes", "")).strip(),
        "lengths": str(payload.get("lengths") if payload.get("lengths") is not None else cur.get("lengths", "")).strip(),
        "sabilEnabled": as_hidden_int(payload.get("sabilEnabled", cur.get("sabilEnabled", 0))),
        "sabilReferenceCode": str(payload.get("sabilReferenceCode") if payload.get("sabilReferenceCode") is not None else cur.get("sabilReferenceCode", "")).strip(),
        "createdAt": as_int(payload.get("createdAt", cur.get("createdAt", now)), now),
    }

    return product


def require_admin() -> tuple[bool, Any]:
    if not API_TOKEN:
        return False, (jsonify({"ok": False, "error": "Server misconfigured: API_TOKEN missing in .env"}), 500)

    auth = str(request.headers.get("Authorization", ""))
    token = ""
    if auth.startswith("Bearer "):
        token = auth[7:].strip()

    if token != API_TOKEN:
        return False, (jsonify({"ok": False, "error": "Unauthorized"}), 401)

    return True, None


@app.get("/admin")
def admin_panel():
    return send_from_directory(ROOT, "admin_panel_v2.html")


@app.get("/uploads/<path:filename>")
def uploads(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "carmenkarla-local-python-server",
        "ts": int(time.time() * 1000),
        "storageMode": "persistent" if _STORAGE_ROOT_ENV else "local",
        "publicBase": _request_public_base(),
    })


@app.get("/products")
def list_products():
    include_hidden = str(request.args.get("includeHidden", "")).strip() == "1"
    products = read_products()
    if not include_hidden:
        products = [p for p in products if as_hidden_int(p.get("isHidden", 0)) == 0]

    products.sort(key=lambda p: as_int(p.get("createdAt", 0)), reverse=True)
    return jsonify({"ok": True, "count": len(products), "items": products})


@app.post("/products/upload")
def upload_image():
    ok, err = require_admin()
    if not ok:
        return err

    if "image" not in request.files:
        return jsonify({"ok": False, "error": "No image uploaded (field name: image)"}), 400

    file = request.files["image"]
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Invalid image file"}), 400

    safe = secure_filename(file.filename)
    safe = re.sub(r"\s+", "_", safe)
    filename = f"{int(time.time() * 1000)}_{safe}"
    dest = UPLOAD_DIR / filename
    file.save(dest)

    url = f"{_request_public_base()}/uploads/{filename}"
    return jsonify({"ok": True, "filename": filename, "url": url})


@app.post("/products")
def add_product():
    ok, err = require_admin()
    if not ok:
        return err

    payload = request.get_json(silent=True) or {}
    products = read_products()
    item = normalize_product(payload)

    if not item["name"]:
        return jsonify({"ok": False, "error": "name is required"}), 400

    if any(str(p.get("id", "")) == item["id"] for p in products):
        return jsonify({"ok": False, "error": "Product id already exists"}), 409

    products.append(item)
    write_products(products)
    return jsonify({"ok": True, "item": item}), 201


@app.put("/products/<pid>")
def update_product(pid: str):
    ok, err = require_admin()
    if not ok:
        return err

    payload = request.get_json(silent=True) or {}
    pid = str(pid).strip()

    products = read_products()
    idx = next((i for i, p in enumerate(products) if str(p.get("id", "")).strip() == pid), -1)
    if idx < 0:
        return jsonify({"ok": False, "error": "Product not found"}), 404

    merged = dict(products[idx])
    merged.update(payload)
    merged["id"] = pid

    item = normalize_product(merged, products[idx])
    if not item["name"]:
        return jsonify({"ok": False, "error": "name is required"}), 400

    products[idx] = item
    write_products(products)
    return jsonify({"ok": True, "item": item})


@app.delete("/products/<pid>")
def delete_product(pid: str):
    ok, err = require_admin()
    if not ok:
        return err

    pid = str(pid).strip()
    products = read_products()
    idx = next((i for i, p in enumerate(products) if str(p.get("id", "")).strip() == pid), -1)
    if idx < 0:
        return jsonify({"ok": False, "error": "Product not found"}), 404

    deleted = products.pop(idx)
    write_products(products)
    return jsonify({"ok": True, "deleted": deleted})


@app.post("/notifications/send")
def send_customer_notifications():
    ok, err = require_admin()
    if not ok:
        return err

    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    audience = str(payload.get("audience") or "all").strip().lower()
    target = str(payload.get("target") or "").strip()
    target_id = str(payload.get("targetId") or "").strip()

    if not title:
        return jsonify({"ok": False, "error": "title is required"}), 400
    if not body:
        return jsonify({"ok": False, "error": "body is required"}), 400

    user_ids: List[str] = []
    if isinstance(payload.get("userIds"), list):
        user_ids = [str(x).strip() for x in payload.get("userIds", []) if str(x).strip()]
    elif payload.get("userId"):
        uid = str(payload.get("userId")).strip()
        if uid:
            user_ids = [uid]

    if audience == "user" and not user_ids:
        return jsonify({"ok": False, "error": "userId/userIds required when audience=user"}), 400

    db, db_error = _firestore_db()

    # Preferred path: Firestore (if available).
    if db:
        if audience == "all":
            limit = as_int(payload.get("limit", 500), 500)
            limit = max(1, min(limit, 2000))
            docs = db.collection("users").limit(limit).stream()
            user_ids = [d.id for d in docs if str(d.id).strip()]

        deduped: List[str] = []
        seen = set()
        for uid in user_ids:
            if uid in seen:
                continue
            seen.add(uid)
            deduped.append(uid)

        if not deduped:
            return jsonify({"ok": False, "error": "No target users found"}), 404

        now_ms = int(time.time() * 1000)
        sent = 0
        chunk_size = 400

        for i in range(0, len(deduped), chunk_size):
            batch = db.batch()
            chunk = deduped[i:i + chunk_size]
            for uid in chunk:
                doc_id = f"n_admin_{now_ms}_{uuid.uuid4().hex[:10]}"
                ref = db.collection("users").document(uid).collection("notifications").document(doc_id)
                batch.set(ref, {
                    "title": title,
                    "body": body,
                    "target": target,
                    "targetId": target_id,
                    "read": False,
                    "createdAtMs": now_ms,
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                    "source": "admin_panel",
                }, merge=True)
            batch.commit()
            sent += len(chunk)

        return jsonify({
            "ok": True,
            "sent": sent,
            "audience": audience,
            "title": title,
            "backend": "firestore",
        })

    # Fallback path: local JSON storage (works without Firebase).
    entries = read_notifications()
    now_ms = int(time.time() * 1000)
    sent = 0

    if audience == "all":
        item = normalize_notification_item({
            "title": title,
            "body": body,
            "target": target,
            "targetId": target_id,
            "audience": "all",
            "uid": "",
            "createdAtMs": now_ms,
        })
        entries.append(item)
        sent = 1
    else:
        deduped: List[str] = []
        seen = set()
        for uid in user_ids:
            if uid in seen:
                continue
            seen.add(uid)
            deduped.append(uid)

        if not deduped:
            return jsonify({"ok": False, "error": "No target users found"}), 404

        for uid in deduped:
            entries.append(normalize_notification_item({
                "title": title,
                "body": body,
                "target": target,
                "targetId": target_id,
                "audience": "user",
                "uid": uid,
                "createdAtMs": now_ms,
            }))
            sent += 1

    entries.sort(key=lambda x: as_int(x.get("createdAtMs", 0)), reverse=True)
    entries = entries[:1000]
    write_notifications(entries)

    return jsonify({
        "ok": True,
        "sent": sent,
        "audience": audience,
        "title": title,
        "backend": "local-file",
        "fallbackReason": db_error,
    })


@app.get("/notifications/feed")
def notifications_feed():
    since_ms = as_int(request.args.get("sinceMs", 0), 0)
    limit = as_int(request.args.get("limit", 50), 50)
    limit = max(1, min(limit, 300))
    uid = str(request.args.get("uid", "") or "").strip()

    items = read_notifications()

    def allowed(item: Dict[str, Any]) -> bool:
        created = as_int(item.get("createdAtMs", 0), 0)
        if created <= since_ms:
            return False

        audience = str(item.get("audience") or "all").strip().lower()
        target_uid = str(item.get("uid") or "").strip()

        if audience == "all":
            return True

        if audience == "user":
            return bool(uid) and uid == target_uid

        return False

    filtered = [normalize_notification_item(x) for x in items if isinstance(x, dict) and allowed(x)]
    filtered.sort(key=lambda x: as_int(x.get("createdAtMs", 0)), reverse=True)
    filtered = filtered[:limit]

    return jsonify({"ok": True, "count": len(filtered), "items": filtered})


if __name__ == "__main__":
    print(f"Local Python server listening on http://{HOST}:{PORT}")
    print("Health: GET /health")
    print("List products: GET /products")
    print("Admin add product: POST /products")
    print("Admin update product: PUT /products/<id>")
    print("Admin delete product: DELETE /products/<id>")
    print("Admin upload image: POST /products/upload (form-data: image)")
    print("Admin send notifications: POST /notifications/send")
    print("Public notifications feed: GET /notifications/feed")
    app.run(host=HOST, port=PORT)

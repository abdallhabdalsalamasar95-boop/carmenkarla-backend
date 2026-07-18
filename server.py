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

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = ROOT / "uploads"
PRODUCTS_FILE = DATA_DIR / "products.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")

HOST = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = int((os.getenv("PORT", "8080") or "8080").strip())
API_TOKEN = (os.getenv("API_TOKEN", "") or "").strip()
CORS_ORIGIN = (os.getenv("CORS_ORIGIN", "") or "").strip()
# Explicit public base URL used for image links (so phone can access them via LAN IP).
# If not set, auto-detected from SERVER_HOST or machine's LAN IP.
_SERVER_BASE_URL_ENV = (os.getenv("SERVER_BASE_URL", "") or "").strip().rstrip("/")


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

PUBLIC_BASE = _resolve_public_base()

if not PRODUCTS_FILE.exists():
    PRODUCTS_FILE.write_text("[]", encoding="utf-8")

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
    return send_from_directory(ROOT, "admin.html")


@app.get("/uploads/<path:filename>")
def uploads(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "carmenkarla-local-python-server", "ts": int(time.time() * 1000)})


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

    url = f"{PUBLIC_BASE}/uploads/{filename}"
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


if __name__ == "__main__":
    print(f"Local Python server listening on http://{HOST}:{PORT}")
    print("Health: GET /health")
    print("List products: GET /products")
    print("Admin add product: POST /products")
    print("Admin update product: PUT /products/<id>")
    print("Admin delete product: DELETE /products/<id>")
    print("Admin upload image: POST /products/upload (form-data: image)")
    app.run(host=HOST, port=PORT)

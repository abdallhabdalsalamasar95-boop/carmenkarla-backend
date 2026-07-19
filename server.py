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


def _resolve_storage_root() -> Path:
    # 1) explicit env always wins
    if _STORAGE_ROOT_ENV:
        p = Path(_STORAGE_ROOT_ENV)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        return p

    # 2) auto-detect persistent volume on hosted Linux (e.g. Render mounted disk)
    persistent_candidates = [Path("/var/data"), Path("/data")]
    for c in persistent_candidates:
        try:
            if c.exists() and c.is_dir():
                return c
        except Exception:
            pass

    # 3) fallback to repo-local storage (development)
    return ROOT


STORAGE_ROOT = _resolve_storage_root()
if not STORAGE_ROOT.is_absolute():
    STORAGE_ROOT = (ROOT / STORAGE_ROOT).resolve()

DATA_DIR = STORAGE_ROOT / "data"
UPLOAD_DIR = STORAGE_ROOT / "uploads"
PRODUCTS_FILE = DATA_DIR / "products.json"
PRODUCTS_BACKUP_DIR = DATA_DIR / "backups"
NOTIFICATIONS_FILE = DATA_DIR / "notifications.json"
DEVICES_FILE = DATA_DIR / "devices.json"
ORDERS_FILE = DATA_DIR / "orders.json"
MARKETING_FILE = DATA_DIR / "marketing.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PRODUCTS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

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
_PRODUCTS_STORAGE_MODE = (os.getenv("PRODUCTS_STORAGE_MODE", "auto") or "auto").strip().lower()
_PRODUCTS_FIRESTORE_COLLECTION = (os.getenv("PRODUCTS_FIRESTORE_COLLECTION", "products_catalog") or "products_catalog").strip()
try:
    _MAX_IMAGE_UPLOAD_MB = max(1, int(float((os.getenv("MAX_IMAGE_UPLOAD_MB", "10") or "10").strip())))
except Exception:
    _MAX_IMAGE_UPLOAD_MB = 10

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

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


def _is_truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _products_firestore_enabled() -> bool:
    mode = _PRODUCTS_STORAGE_MODE
    if mode not in {"auto", "local", "firestore"}:
        mode = "auto"

    if mode == "local":
        return False

    db, _ = _firestore_db()
    if db is None:
        return False

    if mode == "firestore":
        return True

    # auto: Firestore is available => use it as source of truth.
    return True


def _products_backend_label() -> str:
    return "firestore" if _products_firestore_enabled() else "local-file"


def _products_collection_ref() -> Optional[Any]:
    if not _products_firestore_enabled():
        return None
    db, _ = _firestore_db()
    if db is None:
        return None
    return db.collection(_PRODUCTS_FIRESTORE_COLLECTION)


def _write_json_file_atomic(target: Path, value: Any) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _latest_products_backup_path() -> Optional[Path]:
    try:
        backups = sorted(
            PRODUCTS_BACKUP_DIR.glob("products_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return backups[0] if backups else None
    except Exception:
        return None


def _restore_products_from_latest_backup() -> List[Dict[str, Any]]:
    backup = _latest_products_backup_path()
    if backup is None:
        return []
    try:
        raw = backup.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        items = [x for x in data if isinstance(x, dict)]
        if not items:
            return []
        _write_json_file_atomic(PRODUCTS_FILE, items)
        return items
    except Exception:
        return []


def _read_products_local() -> List[Dict[str, Any]]:
    try:
        raw = PRODUCTS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            items = [x for x in data if isinstance(x, dict)]
            auto_restore = _is_truthy(os.getenv("AUTO_RESTORE_EMPTY_PRODUCTS", "1"))
            if auto_restore and not items:
                restored = _restore_products_from_latest_backup()
                if restored:
                    return restored
            return items
        return []
    except Exception:
        return []


def _write_products_local(products: List[Dict[str, Any]]) -> None:
    # Keep a rolling backup history for safety against accidental truncation.
    try:
        if PRODUCTS_FILE.exists():
            stamp = int(time.time() * 1000)
            backup = PRODUCTS_BACKUP_DIR / f"products_{stamp}.json"
            backup.write_text(PRODUCTS_FILE.read_text(encoding="utf-8"), encoding="utf-8")

            backups = sorted(PRODUCTS_BACKUP_DIR.glob("products_*.json"), key=lambda p: p.stat().st_mtime)
            if len(backups) > 20:
                for old in backups[:-20]:
                    try:
                        old.unlink(missing_ok=True)
                    except Exception:
                        pass
    except Exception:
        # Backup is best-effort; never block writes.
        pass

    _write_json_file_atomic(PRODUCTS_FILE, products)


def _read_products_firestore() -> Optional[List[Dict[str, Any]]]:
    ref = _products_collection_ref()
    if ref is None:
        return None

    try:
        out: List[Dict[str, Any]] = []
        for d in ref.stream():
            data = d.to_dict() if hasattr(d, "to_dict") else {}
            if not isinstance(data, dict):
                continue
            row = dict(data)
            row["id"] = str(row.get("id") or d.id).strip()
            if not row["id"]:
                continue
            out.append(row)
        return out
    except Exception:
        return None


def _commit_batched_writes(ops: List[Any]) -> None:
    if not ops:
        return
    chunk = 350
    for i in range(0, len(ops), chunk):
        bops = ops[i:i + chunk]
        batch = _FIRESTORE_DB.batch()
        for op in bops:
            if op["type"] == "set":
                batch.set(op["ref"], op["data"], merge=True)
            elif op["type"] == "delete":
                batch.delete(op["ref"])
        batch.commit()


def _write_products_firestore(products: List[Dict[str, Any]]) -> bool:
    ref = _products_collection_ref()
    if ref is None:
        return False

    try:
        rows: Dict[str, Dict[str, Any]] = {}
        for p in products:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            row = dict(p)
            row["id"] = pid
            rows[pid] = row

        existing_ids = set()
        for d in ref.stream():
            existing_ids.add(str(d.id).strip())

        ops: List[Any] = []
        for pid, row in rows.items():
            ops.append({"type": "set", "ref": ref.document(pid), "data": row})

        for stale_id in sorted(existing_ids - set(rows.keys())):
            if stale_id:
                ops.append({"type": "delete", "ref": ref.document(stale_id)})

        _commit_batched_writes(ops)
        return True
    except Exception:
        return False

PUBLIC_BASE = _resolve_public_base()

if not PRODUCTS_FILE.exists():
    PRODUCTS_FILE.write_text("[]", encoding="utf-8")
else:
    # On boot, recover automatically from latest backup if file was emptied unexpectedly.
    _restore_enabled = _is_truthy(os.getenv("AUTO_RESTORE_EMPTY_PRODUCTS", "1"))
    if _restore_enabled:
        _current_items = _read_products_local()
        if not _current_items:
            _restore_products_from_latest_backup()

if not NOTIFICATIONS_FILE.exists():
    NOTIFICATIONS_FILE.write_text("[]", encoding="utf-8")

if not DEVICES_FILE.exists():
    DEVICES_FILE.write_text("[]", encoding="utf-8")

if not ORDERS_FILE.exists():
    ORDERS_FILE.write_text("[]", encoding="utf-8")


def default_marketing_config() -> Dict[str, Any]:
    return {
        "coupons": [
            {
                "code": "CK10",
                "type": "percent",
                "value": 10.0,
                "minSubtotal": 0.0,
                "maxDiscount": 50.0,
                "freeShipping": 0,
                "enabled": 1,
                "startAt": None,
                "endAt": None,
                "createdAt": int(time.time() * 1000) - 3,
            },
            {
                "code": "CK20",
                "type": "percent",
                "value": 20.0,
                "minSubtotal": 200.0,
                "maxDiscount": 80.0,
                "freeShipping": 0,
                "enabled": 1,
                "startAt": None,
                "endAt": None,
                "createdAt": int(time.time() * 1000) - 2,
            },
            {
                "code": "FREESHIP",
                "type": "freeShipping",
                "value": 0.0,
                "minSubtotal": 0.0,
                "maxDiscount": 0.0,
                "freeShipping": 1,
                "enabled": 1,
                "startAt": None,
                "endAt": None,
                "createdAt": int(time.time() * 1000) - 1,
            },
        ],
        "offers": {
            "title": "💎 عروض لفترة محدودة",
            "subtitle": "تُطبق العروض تلقائياً عند الدفع — والأفضل لك يتفعل مباشرة.",
            "ctaLabel": "تسوقي العروض",
            "items": [
                {"id": "buy2", "text": "اشتري فستانين واحصلي على خصم إضافي 7%", "kind": "discount", "enabled": True, "productIds": []},
                {"id": "gold", "text": "خصم 10% على كل الفساتين الذهبية", "kind": "discount", "enabled": True, "productIds": []},
                {"id": "vip", "text": "تغليف VIP مجاني لمشتريات فوق 400 د.ل", "kind": "vip", "enabled": True, "productIds": []},
                {"id": "ship", "text": "شحن مجاني لمشتريات فوق 250 د.ل داخل ليبيا", "kind": "shipping", "enabled": True, "productIds": []},
            ],
        },
        "gifts": [
            {
                "id": "gift_welcome",
                "title": "هدية الترحيب",
                "description": "أول طلب مؤهل يحصل على هدية رمزية أو تغليف مجاني.",
                "enabled": True,
                "badge": "جديد",
                "ctaLabel": "تسوقي الآن",
                "giftType": "welcome",
                "giftValue": "تغليف مجاني",
                "minOrderTotal": 0.0,
                "imageUrl": "",
            }
        ],
        "competitions": [
            {
                "id": "comp_monthly",
                "title": "مسابقة الشهر",
                "description": "كل عملية شراء مؤهلة تمنح فرصة دخول السحب الشهري.",
                "enabled": True,
                "prize": "قسيمة شراء",
                "ctaLabel": "شاركي الآن",
                "endAt": None,
                "imageUrl": "",
            }
        ],
        "updatedAt": int(time.time() * 1000),
    }


if not MARKETING_FILE.exists():
    _write_json_file_atomic(MARKETING_FILE, default_marketing_config())

app = Flask(__name__)

if CORS_ORIGIN:
    CORS(app, resources={r"/*": {"origins": [CORS_ORIGIN]}})
else:
    CORS(app)


def read_products() -> List[Dict[str, Any]]:
    fs_items = _read_products_firestore()
    products = fs_items if fs_items is not None else _read_products_local()
    normalized, changed = ensure_products_have_codes(products)
    if changed:
        write_products(normalized)
    return normalized


def write_products(products: List[Dict[str, Any]]) -> None:
    # Always keep local file copy + backups.
    _write_products_local(products)

    # If Firestore catalog backend is available, mirror changes there as well.
    _write_products_firestore(products)


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
    _write_json_file_atomic(NOTIFICATIONS_FILE, items)


def read_devices() -> List[Dict[str, Any]]:
    try:
        raw = DEVICES_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:
        return []


def write_devices(items: List[Dict[str, Any]]) -> None:
    _write_json_file_atomic(DEVICES_FILE, items)


def read_orders() -> List[Dict[str, Any]]:
    try:
        raw = ORDERS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:
        return []


def write_orders(items: List[Dict[str, Any]]) -> None:
    _write_json_file_atomic(ORDERS_FILE, items)


def read_marketing_config() -> Dict[str, Any]:
    try:
        raw = MARKETING_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            base = default_marketing_config()
            base.update(data)
            return normalize_marketing_config(base)
    except Exception:
        pass
    return normalize_marketing_config(default_marketing_config())


def write_marketing_config(config: Dict[str, Any]) -> None:
    normalized = normalize_marketing_config(config)
    normalized["updatedAt"] = int(time.time() * 1000)
    _write_json_file_atomic(MARKETING_FILE, normalized)


def normalize_coupon_item(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cur = current or {}
    code = str(payload.get("code") or cur.get("code") or "").strip().upper()
    if not code:
        return None
    ctype = str(payload.get("type") or cur.get("type") or "percent").strip()
    if ctype not in {"percent", "fixed", "freeShipping"}:
        ctype = "percent"
    value = max(0.0, as_number(payload.get("value", cur.get("value", 0.0)), 0.0))
    min_sub = max(0.0, as_number(payload.get("minSubtotal", cur.get("minSubtotal", 0.0)), 0.0))
    max_disc = max(0.0, as_number(payload.get("maxDiscount", cur.get("maxDiscount", 0.0)), 0.0))
    free_shipping = 1 if (as_hidden_int(payload.get("freeShipping", cur.get("freeShipping", 0))) == 1 or ctype == "freeShipping") else 0
    return {
        "code": code,
        "type": ctype,
        "value": value,
        "minSubtotal": min_sub,
        "maxDiscount": max_disc,
        "freeShipping": free_shipping,
        "enabled": as_hidden_int(payload.get("enabled", cur.get("enabled", 1))),
        "startAt": payload.get("startAt", cur.get("startAt")),
        "endAt": payload.get("endAt", cur.get("endAt")),
        "createdAt": as_int(payload.get("createdAt", cur.get("createdAt", int(time.time() * 1000))), int(time.time() * 1000)),
    }


def normalize_offer_item(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cur = current or {}
    oid = str(payload.get("id") or cur.get("id") or "").strip()
    text = str(payload.get("text") or cur.get("text") or "").strip()
    if not oid or not text:
        return None
    return {
        "id": oid,
        "text": text,
        "kind": str(payload.get("kind") or cur.get("kind") or "other").strip(),
        "enabled": bool(payload.get("enabled", cur.get("enabled", True))),
        "productIds": normalize_string_list(payload.get("productIds", cur.get("productIds", []))),
    }


def normalize_campaign_item(payload: Dict[str, Any], *, fallback_id_prefix: str, current: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cur = current or {}
    cid = str(payload.get("id") or cur.get("id") or f"{fallback_id_prefix}_{uuid.uuid4().hex[:8]}").strip()
    title = str(payload.get("title") or cur.get("title") or "").strip()
    description = str(payload.get("description") or cur.get("description") or "").strip()
    if not title:
        return None
    return {
        "id": cid,
        "title": title,
        "description": description,
        "enabled": bool(payload.get("enabled", cur.get("enabled", True))),
        "badge": str(payload.get("badge") or cur.get("badge") or "").strip(),
        "ctaLabel": str(payload.get("ctaLabel") or cur.get("ctaLabel") or "").strip(),
        "giftType": str(payload.get("giftType") or cur.get("giftType") or "").strip(),
        "giftValue": str(payload.get("giftValue") or cur.get("giftValue") or "").strip(),
        "minOrderTotal": max(0.0, as_number(payload.get("minOrderTotal", cur.get("minOrderTotal", 0.0)), 0.0)),
        "prize": str(payload.get("prize") or cur.get("prize") or "").strip(),
        "endAt": payload.get("endAt", cur.get("endAt")),
        "imageUrl": str(payload.get("imageUrl") or cur.get("imageUrl") or "").strip(),
    }


def normalize_marketing_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    raw_coupons = payload.get("coupons") if isinstance(payload.get("coupons"), list) else []
    coupons = [x for x in (normalize_coupon_item(i if isinstance(i, dict) else {}) for i in raw_coupons) if x]

    offers_src = payload.get("offers") if isinstance(payload.get("offers"), dict) else {}
    raw_offers = offers_src.get("items") if isinstance(offers_src.get("items"), list) else []
    offers = [x for x in (normalize_offer_item(i if isinstance(i, dict) else {}) for i in raw_offers) if x]

    raw_gifts = payload.get("gifts") if isinstance(payload.get("gifts"), list) else []
    gifts = [x for x in (normalize_campaign_item(i if isinstance(i, dict) else {}, fallback_id_prefix="gift") for i in raw_gifts) if x]

    raw_competitions = payload.get("competitions") if isinstance(payload.get("competitions"), list) else []
    competitions = [x for x in (normalize_campaign_item(i if isinstance(i, dict) else {}, fallback_id_prefix="competition") for i in raw_competitions) if x]

    return {
        "coupons": coupons,
        "offers": {
            "title": str(offers_src.get("title") or "💎 عروض لفترة محدودة").strip() or "💎 عروض لفترة محدودة",
            "subtitle": str(offers_src.get("subtitle") or "").strip(),
            "ctaLabel": str(offers_src.get("ctaLabel") or "تسوقي العروض").strip() or "تسوقي العروض",
            "items": offers,
        },
        "gifts": gifts,
        "competitions": competitions,
        "updatedAt": as_int(payload.get("updatedAt", now_ms), now_ms),
    }


def public_app_content() -> Dict[str, Any]:
    cfg = read_marketing_config()
    now_ms = int(time.time() * 1000)

    public_coupons = []
    for row in cfg.get("coupons", []):
        if as_hidden_int(row.get("enabled", 1)) != 1:
            continue
        start_at = row.get("startAt")
        end_at = row.get("endAt")
        if start_at is not None and as_int(start_at, 0) > now_ms:
            continue
        if end_at is not None and as_int(end_at, now_ms) < now_ms:
            continue
        public_coupons.append(row)

    public_offers = cfg.get("offers", {})
    public_offers["items"] = [x for x in public_offers.get("items", []) if bool(x.get("enabled", True))]
    public_gifts = [x for x in cfg.get("gifts", []) if bool(x.get("enabled", True))]
    public_competitions = [x for x in cfg.get("competitions", []) if bool(x.get("enabled", True))]

    return {
        "ok": True,
        "updatedAt": cfg.get("updatedAt", now_ms),
        "coupons": public_coupons,
        "offers": public_offers,
        "gifts": public_gifts,
        "competitions": public_competitions,
    }


def normalize_order_item(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = current or {}
    now_ms = int(time.time() * 1000)

    order_id = str(payload.get("orderId") or cur.get("orderId") or f"o_{now_ms}_{uuid.uuid4().hex[:8]}").strip()
    status = str(payload.get("status") or cur.get("status") or "pending").strip().lower()
    if status not in {"pending", "processing", "shipped", "delivered", "canceled"}:
        status = "pending"

    payload_map = payload.get("payload") if isinstance(payload.get("payload"), dict) else cur.get("payload")
    if not isinstance(payload_map, dict):
        payload_map = {}

    customer = payload_map.get("customer") if isinstance(payload_map.get("customer"), dict) else {}
    pricing = payload_map.get("pricing") if isinstance(payload_map.get("pricing"), dict) else {}

    return {
        "orderId": order_id,
        "status": status,
        "uid": str(payload.get("uid") or cur.get("uid") or "").strip(),
        "createdAtMs": as_int(payload.get("createdAtMs", cur.get("createdAtMs", now_ms)), now_ms),
        "updatedAtMs": as_int(payload.get("updatedAtMs", now_ms), now_ms),
        "payload": payload_map,
        "customerName": str(customer.get("name") or cur.get("customerName") or "").strip(),
        "customerPhone": str(customer.get("phone") or cur.get("customerPhone") or "").strip(),
        "customerAddress": str(customer.get("address") or cur.get("customerAddress") or "").strip(),
        "city": str(customer.get("city") or cur.get("city") or "").strip(),
        "grandTotal": as_number(pricing.get("grandTotal", payload_map.get("total", cur.get("grandTotal", 0))), 0),
        "itemsCount": len(payload_map.get("items", [])) if isinstance(payload_map.get("items"), list) else as_int(cur.get("itemsCount", 0), 0),
        "source": str(payload.get("source") or cur.get("source") or "app").strip(),
    }


def normalize_device_item(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = current or {}
    now_ms = int(time.time() * 1000)

    installation_id = str(
        payload.get("installationId")
        or payload.get("deviceId")
        or cur.get("installationId")
        or f"d_{uuid.uuid4().hex[:14]}"
    ).strip()

    platform = str(payload.get("platform") or cur.get("platform") or "unknown").strip().lower()
    device_type = str(payload.get("deviceType") or cur.get("deviceType") or "mobile").strip().lower()

    first_seen = as_int(payload.get("firstSeenMs", cur.get("firstSeenMs", now_ms)), now_ms)
    last_seen = as_int(payload.get("lastSeenMs", cur.get("lastSeenMs", now_ms)), now_ms)
    seen_count = max(1, as_int(payload.get("seenCount", cur.get("seenCount", 1)), 1))

    return {
        "installationId": installation_id,
        "platform": platform,
        "deviceType": device_type,
        "isPhysicalDevice": bool(payload.get("isPhysicalDevice", cur.get("isPhysicalDevice", True))),
        "manufacturer": str(payload.get("manufacturer") or cur.get("manufacturer") or "").strip(),
        "brand": str(payload.get("brand") or cur.get("brand") or "").strip(),
        "device": str(payload.get("device") or cur.get("device") or "").strip(),
        "product": str(payload.get("product") or cur.get("product") or "").strip(),
        "model": str(payload.get("model") or cur.get("model") or "").strip(),
        "sdkInt": as_int(payload.get("sdkInt", cur.get("sdkInt", 0)), 0),
        "systemName": str(payload.get("systemName") or cur.get("systemName") or "").strip(),
        "machine": str(payload.get("machine") or cur.get("machine") or "").strip(),
        "osVersion": str(payload.get("osVersion") or cur.get("osVersion") or "").strip(),
        "appVersion": str(payload.get("appVersion") or cur.get("appVersion") or "").strip(),
        "appBuild": str(payload.get("appBuild") or cur.get("appBuild") or "").strip(),
        "appName": str(payload.get("appName") or cur.get("appName") or "").strip(),
        "locale": str(payload.get("locale") or cur.get("locale") or "").strip(),
        "timezoneOffsetMinutes": as_int(payload.get("timezoneOffsetMinutes", cur.get("timezoneOffsetMinutes", 0)), 0),
        "lastEvent": str(payload.get("event") or cur.get("lastEvent") or "heartbeat").strip(),
        "uid": str(payload.get("uid") or cur.get("uid") or "").strip(),
        "firstSeenMs": first_seen,
        "lastSeenMs": last_seen,
        "seenCount": seen_count,
        "lastIp": str(payload.get("lastIp") or cur.get("lastIp") or "").strip(),
        "userAgent": str(payload.get("userAgent") or cur.get("userAgent") or "").strip(),
    }


def _client_ip() -> str:
    xff = str(request.headers.get("X-Forwarded-For", "") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    cf = str(request.headers.get("CF-Connecting-IP", "") or "").strip()
    if cf:
        return cf
    return str(request.remote_addr or "").strip()


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


def normalize_string_list(value: Any) -> List[str]:
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

    return [x.strip() for x in re.split(r"[,\n\r\t]+", s) if x.strip()]


def normalize_quantity_map(value: Any) -> Dict[str, int]:
    if isinstance(value, dict):
        src = value
    elif value is None:
        src = {}
    else:
        s = str(value).strip()
        if not s:
            src = {}
        elif s.startswith("{"):
            try:
                parsed = json.loads(s)
                src = parsed if isinstance(parsed, dict) else {}
            except Exception:
                src = {}
        else:
            src = {}
            for part in re.split(r"[,\n\r]+", s):
                token = str(part or "").strip()
                if not token or ":" not in token:
                    continue
                key, qty = token.split(":", 1)
                key = key.strip()
                if key:
                    src[key] = qty.strip()

    out: Dict[str, int] = {}
    for raw_key, raw_val in src.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        out[key] = max(0, as_int(raw_val, 0))
    return out


def _quantity_map_total(src: Dict[str, int]) -> int:
    return sum(max(0, as_int(v, 0)) for v in src.values())


def generate_product_code(*, created_at: int, existing_codes: Optional[set[str]] = None) -> str:
    existing = existing_codes or set()
    base = f"CKP-{max(0, created_at) % 1000000:06d}"
    if base not in existing:
        return base
    for idx in range(1, 1000):
        candidate = f"{base}-{idx:03d}"
        if candidate not in existing:
            return candidate
    return f"CKP-{uuid.uuid4().hex[:10].upper()}"


def ensure_products_have_codes(products: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], bool]:
    existing_codes = {
        str(p.get("productCode") or "").strip().upper()
        for p in products
        if isinstance(p, dict) and str(p.get("productCode") or "").strip()
    }
    changed = False
    out: List[Dict[str, Any]] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        row = dict(p)
        code = str(row.get("productCode") or "").strip().upper()
        if not code:
          created_at = as_int(row.get("createdAt", int(time.time() * 1000)), int(time.time() * 1000))
          code = generate_product_code(created_at=created_at, existing_codes=existing_codes)
          row["productCode"] = code
          existing_codes.add(code)
          changed = True
        out.append(row)
    return out, changed


def normalize_product(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = current or {}
    now = int(time.time() * 1000)

    pid = str(payload.get("id") or cur.get("id") or uuid.uuid4()).strip()
    created_at = as_int(payload.get("createdAt", cur.get("createdAt", now)), now)
    name = str(payload.get("name") or cur.get("name") or "").strip()
    description = str(payload.get("description") or cur.get("description") or "").strip()
    category = str(payload.get("category") or cur.get("category") or "غير مصنف").strip()
    tags = str(payload.get("tags") or cur.get("tags") or "").strip()
    product_code = str(payload.get("productCode") or cur.get("productCode") or "").strip().upper()
    if not product_code:
        product_code = generate_product_code(created_at=created_at)

    image_url = str(payload.get("imageUrl") or cur.get("imageUrl") or "").strip()
    image_urls = normalize_image_urls(payload.get("imageUrls", cur.get("imageUrls", [])))
    colors = normalize_string_list(payload.get("colors", cur.get("colors", [])))
    size_quantities = normalize_quantity_map(payload.get("sizeQuantities", cur.get("sizeQuantities", {})))
    color_quantities = normalize_quantity_map(payload.get("colorQuantities", cur.get("colorQuantities", {})))
    stock_quantity = max(0, as_int(payload.get("stockQuantity", cur.get("stockQuantity", 0)), 0))
    low_stock_threshold = max(0, as_int(payload.get("lowStockThreshold", cur.get("lowStockThreshold", 0)), 0))

    if not image_url and image_urls:
        image_url = image_urls[0]

    available_stock = stock_quantity
    if available_stock <= 0:
        size_total = _quantity_map_total(size_quantities)
        color_total = _quantity_map_total(color_quantities)
        available_stock = size_total if size_total > 0 else color_total

    has_inventory_tracking = (
        payload.get("stockQuantity") is not None
        or cur.get("stockQuantity") is not None
        or bool(size_quantities)
        or bool(color_quantities)
    )
    out_of_stock = has_inventory_tracking and available_stock <= 0
    low_stock = (not out_of_stock) and low_stock_threshold > 0 and available_stock <= low_stock_threshold
    low_stock_sizes = [k for k, v in size_quantities.items() if low_stock_threshold > 0 and v <= low_stock_threshold]
    low_stock_colors = [k for k, v in color_quantities.items() if low_stock_threshold > 0 and v <= low_stock_threshold]

    product = {
        "id": pid,
        "productCode": product_code,
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
        "colors": ",".join(colors),
        "stockQuantity": stock_quantity,
        "lowStockThreshold": low_stock_threshold,
        "sizeQuantities": size_quantities,
        "colorQuantities": color_quantities,
        "availableStock": available_stock,
        "outOfStock": 1 if out_of_stock else 0,
        "lowStock": 1 if low_stock else 0,
        "lowStockSizes": low_stock_sizes,
        "lowStockColors": low_stock_colors,
        "sabilEnabled": as_hidden_int(payload.get("sabilEnabled", cur.get("sabilEnabled", 0))),
        "sabilReferenceCode": str(payload.get("sabilReferenceCode") if payload.get("sabilReferenceCode") is not None else cur.get("sabilReferenceCode", "")).strip(),
        "createdAt": created_at,
        "updatedAt": as_int(payload.get("updatedAt", now), now),
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


def _is_valid_api_token_from_request() -> bool:
    if not API_TOKEN:
        return False
    auth = str(request.headers.get("Authorization", "") or "").strip()
    if not auth.startswith("Bearer "):
        return False
    return auth[7:].strip() == API_TOKEN


@app.get("/admin")
def admin_panel():
    return send_from_directory(ROOT, "admin_panel_v2.html")


@app.get("/uploads/<path:filename>")
def uploads(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/health")
def health():
    backend = _products_backend_label()
    storage_mode = "persistent" if (str(STORAGE_ROOT).startswith("/var/data") or str(STORAGE_ROOT).startswith("/data") or _STORAGE_ROOT_ENV) else "local"
    production_ready = backend == "firestore" or storage_mode == "persistent"
    return jsonify({
        "ok": True,
        "service": "carmenkarla-local-python-server",
        "ts": int(time.time() * 1000),
        "storageMode": storage_mode,
        "storageRoot": str(STORAGE_ROOT),
        "catalogBackend": backend,
        "productionReady": production_ready,
        "publicBase": _request_public_base(),
    })


@app.get("/marketing/config")
def get_marketing_config():
    ok, err = require_admin()
    if not ok:
        return err
    return jsonify({"ok": True, "config": read_marketing_config()})


@app.put("/marketing/config")
def update_marketing_config():
    ok, err = require_admin()
    if not ok:
        return err
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid config payload"}), 400
    write_marketing_config(payload)
    return jsonify({"ok": True, "config": read_marketing_config()})


@app.get("/app/content")
def app_content():
    return jsonify(public_app_content())


@app.post("/devices/register")
def register_device_installation():
    payload = request.get_json(silent=True) or {}
    installation_id = str(payload.get("installationId") or payload.get("deviceId") or "").strip()
    if not installation_id:
        return jsonify({"ok": False, "error": "installationId is required"}), 400

    entries = read_devices()
    now_ms = int(time.time() * 1000)

    idx = next(
        (i for i, d in enumerate(entries) if str(d.get("installationId", "")).strip() == installation_id),
        -1,
    )

    item_payload = dict(payload)
    item_payload["installationId"] = installation_id
    item_payload["lastSeenMs"] = now_ms
    item_payload["lastIp"] = _client_ip()
    item_payload["userAgent"] = str(request.headers.get("User-Agent", "") or "").strip()

    created = idx < 0
    if created:
        item_payload["firstSeenMs"] = now_ms
        item_payload["seenCount"] = 1
        item = normalize_device_item(item_payload)
        entries.append(item)
    else:
        current = entries[idx]
        item_payload["firstSeenMs"] = as_int(current.get("firstSeenMs", now_ms), now_ms)
        item_payload["seenCount"] = as_int(current.get("seenCount", 1), 1) + 1
        item = normalize_device_item(item_payload, current)
        entries[idx] = item

    entries.sort(key=lambda x: as_int(x.get("lastSeenMs", 0), 0), reverse=True)
    entries = entries[:20000]
    write_devices(entries)

    return jsonify({
        "ok": True,
        "created": created,
        "installationId": item.get("installationId", ""),
        "lastSeenMs": item.get("lastSeenMs", now_ms),
    })


@app.get("/devices/stats")
def devices_stats():
    ok, err = require_admin()
    if not ok:
        return err

    days = as_int(request.args.get("days", 30), 30)
    days = max(1, min(days, 365))
    limit = as_int(request.args.get("limit", 200), 200)
    limit = max(1, min(limit, 1000))

    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - (days * 24 * 60 * 60 * 1000)
    cutoff_1d = now_ms - (1 * 24 * 60 * 60 * 1000)
    cutoff_7d = now_ms - (7 * 24 * 60 * 60 * 1000)
    cutoff_30d = now_ms - (30 * 24 * 60 * 60 * 1000)

    entries = read_devices()

    platform_counts: Dict[str, int] = {}
    type_counts: Dict[str, int] = {}
    model_counts: Dict[str, int] = {}

    active_count = 0
    active_1d = 0
    active_7d = 0
    active_30d = 0
    for d in entries:
        platform = str(d.get("platform") or "unknown").strip().lower() or "unknown"
        dtype = str(d.get("deviceType") or "mobile").strip().lower() or "mobile"
        brand = str(d.get("brand") or "").strip()
        model = str(d.get("model") or "").strip()
        model_key = (f"{brand} {model}".strip() or model or "Unknown")

        platform_counts[platform] = platform_counts.get(platform, 0) + 1
        type_counts[dtype] = type_counts.get(dtype, 0) + 1
        model_counts[model_key] = model_counts.get(model_key, 0) + 1

        last_seen = as_int(d.get("lastSeenMs", 0), 0)
        if last_seen >= cutoff_ms:
            active_count += 1
        if last_seen >= cutoff_1d:
            active_1d += 1
        if last_seen >= cutoff_7d:
            active_7d += 1
        if last_seen >= cutoff_30d:
            active_30d += 1

    def as_sorted_counts(src: Dict[str, int]) -> List[Dict[str, Any]]:
        return [
            {"name": k, "count": v}
            for k, v in sorted(src.items(), key=lambda kv: kv[1], reverse=True)
        ]

    sorted_entries = sorted(
        entries,
        key=lambda x: as_int(x.get("lastSeenMs", 0), 0),
        reverse=True,
    )

    recent_items = [
        {
            "installationId": str(d.get("installationId") or ""),
            "platform": str(d.get("platform") or "unknown"),
            "deviceType": str(d.get("deviceType") or "mobile"),
            "brand": str(d.get("brand") or ""),
            "model": str(d.get("model") or ""),
            "osVersion": str(d.get("osVersion") or ""),
            "locale": str(d.get("locale") or ""),
            "uid": str(d.get("uid") or ""),
            "appVersion": str(d.get("appVersion") or ""),
            "appBuild": str(d.get("appBuild") or ""),
            "lastIp": str(d.get("lastIp") or ""),
            "lastEvent": str(d.get("lastEvent") or ""),
            "firstSeenMs": as_int(d.get("firstSeenMs", 0), 0),
            "lastSeenMs": as_int(d.get("lastSeenMs", 0), 0),
            "seenCount": as_int(d.get("seenCount", 1), 1),
        }
        for d in sorted_entries[:limit]
    ]

    return jsonify({
        "ok": True,
        "totalInstalled": len(entries),
        "activeDevices": active_count,
        "activeWindowDays": days,
        "active1d": active_1d,
        "active7d": active_7d,
        "active30d": active_30d,
        "platforms": as_sorted_counts(platform_counts),
        "deviceTypes": as_sorted_counts(type_counts),
        "topModels": as_sorted_counts(model_counts)[:10],
        "recentItems": recent_items,
    })


@app.get("/dashboard/summary")
def dashboard_summary():
    ok, err = require_admin()
    if not ok:
        return err

    now_ms = int(time.time() * 1000)
    cutoff_1d = now_ms - (1 * 24 * 60 * 60 * 1000)
    cutoff_7d = now_ms - (7 * 24 * 60 * 60 * 1000)
    cutoff_30d = now_ms - (30 * 24 * 60 * 60 * 1000)

    products = read_products()
    total_products = len(products)
    visible_products = len([p for p in products if as_hidden_int(p.get("isHidden", 0)) == 0])
    hidden_products = total_products - visible_products
    categories = len({str(p.get("category") or "").strip() for p in products if str(p.get("category") or "").strip()})
    low_stock_count = len([p for p in products if as_hidden_int(p.get("lowStock", 0)) == 1])
    out_of_stock_count = len([p for p in products if as_hidden_int(p.get("outOfStock", 0)) == 1])

    orders = [normalize_order_item(x) for x in read_orders() if isinstance(x, dict)]
    orders_total = len(orders)
    status_counts = {
        "pending": 0,
        "processing": 0,
        "shipped": 0,
        "delivered": 0,
        "canceled": 0,
    }
    unique_order_customers = set()
    for o in orders:
        st = str(o.get("status") or "pending").strip().lower()
        if st in status_counts:
            status_counts[st] += 1
        phone = str(o.get("customerPhone") or "").strip()
        if phone:
            unique_order_customers.add(phone)

    devices = read_devices()
    total_installed = len(devices)
    registered_users = {str(d.get("uid") or "").strip() for d in devices if str(d.get("uid") or "").strip()}

    active_users_1d = {
        str(d.get("uid") or "").strip()
        for d in devices
        if str(d.get("uid") or "").strip() and as_int(d.get("lastSeenMs", 0), 0) >= cutoff_1d
    }
    active_users_7d = {
        str(d.get("uid") or "").strip()
        for d in devices
        if str(d.get("uid") or "").strip() and as_int(d.get("lastSeenMs", 0), 0) >= cutoff_7d
    }
    active_users_30d = {
        str(d.get("uid") or "").strip()
        for d in devices
        if str(d.get("uid") or "").strip() and as_int(d.get("lastSeenMs", 0), 0) >= cutoff_30d
    }

    return jsonify({
        "ok": True,
        "ts": now_ms,
        "products": {
            "total": total_products,
            "visible": visible_products,
            "hidden": hidden_products,
            "categories": categories,
            "lowStock": low_stock_count,
            "outOfStock": out_of_stock_count,
        },
        "orders": {
            "total": orders_total,
            "pending": status_counts["pending"],
            "processing": status_counts["processing"],
            "shipped": status_counts["shipped"],
            "delivered": status_counts["delivered"],
            "canceled": status_counts["canceled"],
            "uniqueCustomers": len(unique_order_customers),
        },
        "users": {
            "registered": len(registered_users),
            "active1d": len(active_users_1d),
            "active7d": len(active_users_7d),
            "active30d": len(active_users_30d),
        },
        "devices": {
            "installed": total_installed,
        },
    })


@app.get("/products")
def list_products():
    include_hidden = str(request.args.get("includeHidden", "")).strip() == "1"
    products = read_products()
    if not include_hidden:
        products = [p for p in products if as_hidden_int(p.get("isHidden", 0)) == 0]

    products.sort(key=lambda p: as_int(p.get("createdAt", 0)), reverse=True)
    return jsonify({"ok": True, "count": len(products), "items": products})


@app.post("/orders")
def create_order_from_app():
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("orderId") or "").strip()
    order_payload = payload.get("payload")
    if not order_id:
        return jsonify({"ok": False, "error": "orderId is required"}), 400
    if not isinstance(order_payload, dict):
        return jsonify({"ok": False, "error": "payload is required"}), 400

    entries = read_orders()
    idx = next((i for i, o in enumerate(entries) if str(o.get("orderId", "")).strip() == order_id), -1)

    item_payload = dict(payload)
    item_payload["orderId"] = order_id
    item_payload["source"] = "app"
    item_payload["updatedAtMs"] = int(time.time() * 1000)

    created = idx < 0
    if created:
        item = normalize_order_item(item_payload)
        entries.append(item)
    else:
        item = normalize_order_item(item_payload, entries[idx])
        entries[idx] = item

    entries.sort(key=lambda x: as_int(x.get("createdAtMs", 0), 0), reverse=True)
    entries = entries[:5000]
    write_orders(entries)

    return jsonify({"ok": True, "created": created, "orderId": order_id, "status": item.get("status", "pending")})


@app.get("/orders")
def list_orders():
    ok, err = require_admin()
    if not ok:
        return err

    limit = as_int(request.args.get("limit", 200), 200)
    limit = max(1, min(limit, 1000))
    status = str(request.args.get("status", "") or "").strip().lower()

    items = [normalize_order_item(x) for x in read_orders() if isinstance(x, dict)]
    if status:
        items = [x for x in items if str(x.get("status", "")).strip().lower() == status]

    items.sort(key=lambda x: as_int(x.get("createdAtMs", 0), 0), reverse=True)
    items = items[:limit]
    return jsonify({"ok": True, "count": len(items), "items": items})


@app.put("/orders/<order_id>/status")
def update_order_status(order_id: str):
    ok, err = require_admin()
    if not ok:
        return err

    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "").strip().lower()
    allowed = {"pending", "processing", "shipped", "delivered", "canceled"}
    if status not in allowed:
        return jsonify({"ok": False, "error": f"status must be one of {sorted(allowed)}"}), 400

    order_id = str(order_id or "").strip()
    entries = read_orders()
    idx = next((i for i, o in enumerate(entries) if str(o.get("orderId", "")).strip() == order_id), -1)
    if idx < 0:
        return jsonify({"ok": False, "error": "Order not found"}), 404

    current = entries[idx]
    merged = dict(current)
    merged["status"] = status
    merged["updatedAtMs"] = int(time.time() * 1000)
    item = normalize_order_item(merged, current)
    entries[idx] = item
    write_orders(entries)

    return jsonify({"ok": True, "item": item})


@app.post("/products/upload")
def upload_image():
    ok, err = require_admin()
    if not ok:
        return err

    file = None
    for field_name in ("image", "file", "files[]", "images[]"):
        if field_name in request.files:
            file = request.files[field_name]
            break

    if file is None:
        return jsonify({"ok": False, "error": "No image uploaded (supported fields: image, file, files[], images[])"}), 400

    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Invalid image file"}), 400

    safe = secure_filename(file.filename)
    safe = re.sub(r"\s+", "_", safe)
    ext = Path(safe).suffix.lower().strip()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify({"ok": False, "error": f"Unsupported image type: {ext or 'unknown'}"}), 400

    mime = str(getattr(file, "mimetype", "") or "").strip().lower()
    if mime and not mime.startswith("image/"):
        return jsonify({"ok": False, "error": f"Invalid image MIME type: {mime}"}), 400

    max_bytes = _MAX_IMAGE_UPLOAD_MB * 1024 * 1024
    content_length = as_int(request.content_length or 0, 0)
    if content_length > max_bytes:
        return jsonify({"ok": False, "error": f"Image exceeds max size of {_MAX_IMAGE_UPLOAD_MB}MB"}), 413

    filename = f"{int(time.time() * 1000)}_{safe}"
    dest = UPLOAD_DIR / filename
    file.save(dest)

    url = f"{_request_public_base()}/uploads/{filename}"
    return jsonify({"ok": True, "filename": filename, "url": url, "sizeBytes": dest.stat().st_size if dest.exists() else 0})


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
    print("Public device register: POST /devices/register")
    print("Admin devices stats: GET /devices/stats")
    print("App create order: POST /orders")
    print("Admin list orders: GET /orders")
    print("Admin update order status: PUT /orders/<id>/status")
    app.run(host=HOST, port=PORT)

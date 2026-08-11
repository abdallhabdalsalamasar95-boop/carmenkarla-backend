import base64
import json
import hashlib
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

_FIREBASE_IMPORT_ERROR = ""

try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth, credentials, firestore
except Exception as _ex:  # pragma: no cover - optional dependency at runtime
    firebase_admin = None
    firebase_auth = None
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
LOOKS_FILE = DATA_DIR / "looks.json"
NOTIFICATIONS_FILE = DATA_DIR / "notifications.json"
DEVICES_FILE = DATA_DIR / "devices.json"
ORDERS_FILE = DATA_DIR / "orders.json"
AMBASSADOR_WITHDRAWALS_FILE = DATA_DIR / "ambassador_withdrawals.json"
EXPENSES_FILE = DATA_DIR / "expenses.json"
MARKETING_FILE = DATA_DIR / "marketing.json"
AMBASSADOR_WITHDRAWAL_MINIMUM = 100.0

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
_LOOKS_FIRESTORE_COLLECTION = (os.getenv("LOOKS_FIRESTORE_COLLECTION", "complete_looks") or "complete_looks").strip()
_SABIL_ENABLED = str(os.getenv("SABIL_ENABLED", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
_SABIL_API_BASE_URL = (os.getenv("SABIL_API_BASE_URL", "https://v2.sabil.ly") or "https://v2.sabil.ly").strip().rstrip("/")
_SABIL_CREATE_SHIPMENT_PATH = (os.getenv("SABIL_CREATE_SHIPMENT_PATH", "/api/local/shipments") or "/api/local/shipments").strip()
_SABIL_API_KEY = (os.getenv("SABIL_API_KEY", "") or "").strip()
_SABIL_ACCESS_TOKEN = (os.getenv("SABIL_ACCESS_TOKEN", "") or "").strip()
_SABIL_REFRESH_TOKEN = (os.getenv("SABIL_REFRESH_TOKEN", "") or "").strip()
_SABIL_REFRESH_PATH = (os.getenv("SABIL_REFRESH_PATH", "/api/oauth/refresh/") or "/api/oauth/refresh/").strip()
_SABIL_ACCOUNT_ID = (os.getenv("SABIL_ACCOUNT_ID", "") or "").strip()
_SABIL_API_VERSION = (os.getenv("SABIL_API_VERSION", "1.0.0") or "1.0.0").strip()
_SABIL_SERVICE_ID = (os.getenv("SABIL_SERVICE_ID", "") or "").strip()
_SABIL_CONTACTS_PATH = (os.getenv("SABIL_CONTACTS_PATH", "/api/contacts") or "/api/contacts").strip()
_SABIL_CONTACT_IDS = [item.strip() for item in (os.getenv("SABIL_CONTACT_IDS", "") or "").split(",") if item.strip()]
_SABIL_PAYMENT_BY = (os.getenv("SABIL_PAYMENT_BY", "receiver") or "receiver").strip().lower()
_SABIL_COUNTRY_CODE = (os.getenv("SABIL_COUNTRY_CODE", "lby") or "lby").strip().lower()
_SABIL_DEFAULT_AREA = (os.getenv("SABIL_DEFAULT_AREA", "") or "").strip()
_SABIL_CURRENCY = (os.getenv("SABIL_CURRENCY", "lyd") or "lyd").strip().lower()
_SABIL_USER_AGENT = (os.getenv(
    "SABIL_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Code/1.132.0 Chrome/148.0.7778.280 "
    "Electron/42.7.1 Safari/537.36",
) or "").strip()
_SABIL_SESSION_FILE = DATA_DIR / "sabil_session.json"
_SABIL_SESSION_LOCK = threading.Lock()
try:
    _MAX_IMAGE_UPLOAD_MB = max(1, int(float((os.getenv("MAX_IMAGE_UPLOAD_MB", "10") or "10").strip())))
except Exception:
    _MAX_IMAGE_UPLOAD_MB = 10

ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".jfif",
    ".heic",
    ".heif",
    ".avif",
}

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


def _firebase_user_from_request() -> tuple[Optional[Dict[str, Any]], Optional[Any]]:
    auth_header = str(request.headers.get("Authorization", "") or "").strip()
    if not auth_header.startswith("Bearer "):
        return None, (jsonify({"ok": False, "error": "تسجيل الدخول مطلوب"}), 401)
    token = auth_header[7:].strip()
    if not token or firebase_auth is None:
        return None, (jsonify({"ok": False, "error": "تعذر التحقق من الحساب"}), 401)
    try:
        _init_firestore()
        decoded = firebase_auth.verify_id_token(token)
        uid = str(decoded.get("uid") or decoded.get("sub") or "").strip()
        if not uid:
            raise ValueError("Token has no uid")
        decoded["uid"] = uid
        return decoded, None
    except Exception:
        return None, (jsonify({"ok": False, "error": "انتهت جلسة الدخول، سجّلي الدخول مجددًا"}), 401)


def _firebase_user_profile(uid: str) -> Dict[str, Any]:
    db, _ = _firestore_db()
    if db is None:
        return {}
    try:
        snapshot = db.collection("users").document(uid).get()
        data = snapshot.to_dict() if getattr(snapshot, "exists", False) else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_firebase_user_profile(uid: str, profile: Dict[str, Any]) -> tuple[bool, str]:
    db, db_error = _firestore_db()
    if db is None:
        return False, db_error or "تعذر الاتصال بقاعدة البيانات"
    try:
        db.collection("users").document(uid).set(profile, merge=True)
        return True, ""
    except Exception as ex:
        return False, str(ex)


def _firebase_ambassador_profiles() -> tuple[List[Dict[str, Any]], str]:
    db, db_error = _firestore_db()
    if db is None:
        return [], db_error or "تعذر الاتصال بقاعدة بيانات الحسابات"
    try:
        items: List[Dict[str, Any]] = []
        for snapshot in db.collection("users").stream():
            data = snapshot.to_dict()
            if not isinstance(data, dict):
                continue
            if str(data.get("accountRole") or "").strip().lower() != "ambassador":
                continue
            items.append({
                "uid": str(data.get("uid") or snapshot.id or "").strip(),
                "accountRole": "ambassador",
                "ambassadorName": str(data.get("ambassadorName") or data.get("name") or "").strip(),
                "ambassadorPhone": str(data.get("ambassadorPhone") or data.get("phone") or "").strip(),
                "ambassadorAddress": str(data.get("ambassadorAddress") or data.get("address") or "").strip(),
                "email": str(data.get("email") or "").strip(),
                "status": str(data.get("status") or "active").strip().lower(),
                "joinedAt": as_int(data.get("joinedAt"), 0),
                "updatedAt": as_int(data.get("updatedAt"), 0),
            })
        items.sort(key=lambda item: as_int(item.get("joinedAt"), 0), reverse=True)
        return items, ""
    except Exception as ex:
        return [], str(ex)


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


def _looks_collection_ref() -> Optional[Any]:
    db, _ = _firestore_db()
    if db is None:
        return None
    return db.collection(_LOOKS_FIRESTORE_COLLECTION)


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


def _read_looks_local() -> List[Dict[str, Any]]:
    try:
        raw = LOOKS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:
        return []


def _write_looks_local(items: List[Dict[str, Any]]) -> None:
    _write_json_file_atomic(LOOKS_FILE, items)


def _read_looks_firestore() -> Optional[List[Dict[str, Any]]]:
    ref = _looks_collection_ref()
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
            if row["id"]:
                out.append(row)
        return out
    except Exception:
        return None


def _write_looks_firestore(items: List[Dict[str, Any]]) -> bool:
    ref = _looks_collection_ref()
    if ref is None:
        return False
    try:
        rows: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            row = dict(item)
            row["id"] = item_id
            rows[item_id] = row

        existing_ids = set()
        for d in ref.stream():
            existing_ids.add(str(d.id).strip())

        ops: List[Any] = []
        for item_id, row in rows.items():
            ops.append({"type": "set", "ref": ref.document(item_id), "data": row})
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

if not LOOKS_FILE.exists():
    LOOKS_FILE.write_text("[]", encoding="utf-8")

if not NOTIFICATIONS_FILE.exists():
    NOTIFICATIONS_FILE.write_text("[]", encoding="utf-8")

if not DEVICES_FILE.exists():
    DEVICES_FILE.write_text("[]", encoding="utf-8")

if not ORDERS_FILE.exists():
    ORDERS_FILE.write_text("[]", encoding="utf-8")

if not AMBASSADOR_WITHDRAWALS_FILE.exists():
    AMBASSADOR_WITHDRAWALS_FILE.write_text("[]", encoding="utf-8")

if not EXPENSES_FILE.exists():
    EXPENSES_FILE.write_text("[]", encoding="utf-8")


def default_marketing_config() -> Dict[str, Any]:
    return {
        "websiteHome": {
            "banner": {
                "imageUrl": "",
                "altText": "بانر أڤيا فاشن",
                "linkUrl": "#collection",
                "enabled": True,
            },
            "categories": [],
        },
        "commission": {
            "defaultPercent": 7.0,
            "perProductEnabled": True,
        },
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
_INVENTORY_LOCK = threading.RLock()
_SABIL_CONTACT_LOCK = threading.Lock()
_WITHDRAWAL_LOCK = threading.RLock()
_EXPENSES_LOCK = threading.RLock()

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


def read_looks() -> List[Dict[str, Any]]:
    fs_items = _read_looks_firestore()
    items = fs_items if fs_items is not None else _read_looks_local()
    normalized = [normalize_look_item(x) for x in items if isinstance(x, dict)]
    normalized.sort(key=lambda x: as_int(x.get("sortOrder", x.get("createdAt", 0)), 0), reverse=True)
    return normalized


def write_looks(items: List[Dict[str, Any]]) -> None:
    _write_looks_local(items)
    _write_looks_firestore(items)


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


def read_ambassador_withdrawals() -> List[Dict[str, Any]]:
    try:
        raw = AMBASSADOR_WITHDRAWALS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:
        return []


def write_ambassador_withdrawals(items: List[Dict[str, Any]]) -> None:
    _write_json_file_atomic(AMBASSADOR_WITHDRAWALS_FILE, items)


def read_expenses() -> List[Dict[str, Any]]:
    try:
        raw = EXPENSES_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:
        return []


def write_expenses(items: List[Dict[str, Any]]) -> None:
    _write_json_file_atomic(EXPENSES_FILE, items)


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


def normalize_website_category(payload: Dict[str, Any], index: int = 0) -> Optional[Dict[str, Any]]:
    title = str(payload.get("title") or "").strip()
    image_url = str(payload.get("imageUrl") or "").strip()
    if not title:
        return None
    return {
        "id": str(payload.get("id") or f"category_{uuid.uuid4().hex[:8]}").strip(),
        "title": title,
        "imageUrl": image_url,
        "productCategoryFilter": str(payload.get("productCategoryFilter") or "").strip(),
        "enabled": bool(payload.get("enabled", True)),
        "sortOrder": as_int(payload.get("sortOrder", index), index),
    }


def normalize_website_home(payload: Any) -> Dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    banner_source = source.get("banner") if isinstance(source.get("banner"), dict) else {}
    raw_categories = source.get("categories") if isinstance(source.get("categories"), list) else []
    categories = [
        item
        for item in (
            normalize_website_category(row if isinstance(row, dict) else {}, index)
            for index, row in enumerate(raw_categories[:12])
        )
        if item
    ]
    categories.sort(key=lambda item: as_int(item.get("sortOrder", 0), 0))
    return {
        "banner": {
            "imageUrl": str(banner_source.get("imageUrl") or "").strip(),
            "altText": str(banner_source.get("altText") or "بانر أڤيا فاشن").strip() or "بانر أڤيا فاشن",
            "linkUrl": str(banner_source.get("linkUrl") or "#collection").strip(),
            "enabled": bool(banner_source.get("enabled", True)),
        },
        "categories": categories,
    }


def normalize_marketing_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    commission_src = payload.get("commission") if isinstance(payload.get("commission"), dict) else {}
    commission_default = as_number(commission_src.get("defaultPercent", 7.0), 7.0)
    commission_default = max(0.0, min(100.0, commission_default))
    commission_per_product = bool(commission_src.get("perProductEnabled", True))

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
        "websiteHome": normalize_website_home(payload.get("websiteHome")),
        "commission": {
            "defaultPercent": commission_default,
            "perProductEnabled": commission_per_product,
        },
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
    commission_cfg = cfg.get("commission") if isinstance(cfg.get("commission"), dict) else {}
    commission_default = as_number(commission_cfg.get("defaultPercent", 7.0), 7.0)
    commission_default = max(0.0, min(100.0, commission_default))
    commission_per_product = bool(commission_cfg.get("perProductEnabled", True))

    return {
        "ok": True,
        "updatedAt": cfg.get("updatedAt", now_ms),
        "websiteHome": {
            "banner": cfg.get("websiteHome", {}).get("banner", {}),
            "categories": [
                item
                for item in cfg.get("websiteHome", {}).get("categories", [])
                if bool(item.get("enabled", True))
            ],
        },
        "commission": {
            "defaultPercent": commission_default,
            "perProductEnabled": commission_per_product,
        },
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
    ambassador_summary = payload.get("ambassadorSummary")
    if not isinstance(ambassador_summary, dict):
        ambassador_summary = payload_map.get("ambassadorSummary")
    if not isinstance(ambassador_summary, dict):
        ambassador_summary = cur.get("ambassadorSummary")
    if not isinstance(ambassador_summary, dict):
        ambassador_summary = {}
    ambassador_summary = dict(ambassador_summary)
    external_delivery = payload.get("externalDelivery")
    if not isinstance(external_delivery, dict):
        external_delivery = cur.get("externalDelivery")
    if not isinstance(external_delivery, dict):
        external_delivery = {}

    is_ambassador_order = (
        bool(ambassador_summary.get("isAmbassadorOrder"))
        or bool(customer.get("placedAsAmbassador"))
        or str(customer.get("accountRole") or "").strip().lower() == "ambassador"
    )
    if is_ambassador_order:
        ambassador_summary["isAmbassadorOrder"] = True
        identity_fields = {
            "ambassadorUid": customer.get("submitterUid") or customer.get("uid"),
            "ambassadorName": (
                customer.get("submitterName")
                or customer.get("accountName")
                or customer.get("displayName")
                or customer.get("fullName")
            ),
            "ambassadorEmail": customer.get("submitterEmail") or customer.get("email"),
            "ambassadorPhone": (
                customer.get("submitterPhone")
                or customer.get("accountPhone")
                or customer.get("ambassadorPhone")
            ),
        }
        for key, value in identity_fields.items():
            if not str(ambassador_summary.get(key) or "").strip() and str(value or "").strip():
                ambassador_summary[key] = str(value).strip()

    return {
        "orderId": order_id,
        "status": status,
        "uid": str(payload.get("uid") or cur.get("uid") or "").strip(),
        "createdAtMs": as_int(payload.get("createdAtMs", cur.get("createdAtMs", now_ms)), now_ms),
        "updatedAtMs": as_int(payload.get("updatedAtMs", now_ms), now_ms),
        "payload": payload_map,
        "ambassadorSummary": ambassador_summary,
        "customerName": str(customer.get("name") or cur.get("customerName") or "").strip(),
        "customerPhone": str(customer.get("phone") or cur.get("customerPhone") or "").strip(),
        "customerAddress": str(customer.get("address") or cur.get("customerAddress") or "").strip(),
        "city": str(customer.get("city") or cur.get("city") or "").strip(),
        "grandTotal": as_number(pricing.get("grandTotal", payload_map.get("total", cur.get("grandTotal", 0))), 0),
        "itemsCount": (
            sum(max(0, as_int(line.get("quantity", 0), 0)) for line in payload_map.get("items", []) if isinstance(line, dict))
            if isinstance(payload_map.get("items"), list)
            else as_int(cur.get("itemsCount", 0), 0)
        ),
        "source": str(payload.get("source") or cur.get("source") or "app").strip(),
        "inventoryReserved": bool(payload.get("inventoryReserved", cur.get("inventoryReserved", False))),
        "inventoryReservation": (
            payload.get("inventoryReservation")
            if isinstance(payload.get("inventoryReservation"), list)
            else cur.get("inventoryReservation", [])
        ),
        "externalDelivery": dict(external_delivery),
    }


def sabil_config_status() -> Dict[str, Any]:
    missing = []
    for key, value in {
        "SABIL_AUTH": _SABIL_ACCESS_TOKEN or _SABIL_API_KEY,
        "SABIL_ACCOUNT_ID": _SABIL_ACCOUNT_ID,
        "SABIL_SERVICE_ID": _SABIL_SERVICE_ID,
    }.items():
        if not value:
            missing.append(key)
    return {
        "provider": "darb_sabeel",
        "enabled": _SABIL_ENABLED,
        "configured": not missing,
        "ready": _SABIL_ENABLED and not missing,
        "missing": missing,
        "authMode": "session" if _SABIL_ACCESS_TOKEN else ("api_key" if _SABIL_API_KEY else ""),
        "sessionRefreshConfigured": bool(_SABIL_REFRESH_TOKEN),
        "endpoint": f"{_SABIL_API_BASE_URL}{_SABIL_CREATE_SHIPMENT_PATH}",
    }


def _first_nested_value(data: Any, keys: set[str]) -> str:
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in keys and value is not None and not isinstance(value, (dict, list)):
                text = str(value).strip()
                if text:
                    return text
        for value in data.values():
            found = _first_nested_value(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _first_nested_value(value, keys)
            if found:
                return found
    return ""


def build_sabil_shipment_payload(
    order: Dict[str, Any],
    contact_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    city = str(customer.get("city") or order.get("city") or "").strip()
    address = str(customer.get("address") or order.get("customerAddress") or "").strip()
    area = str(customer.get("area") or _SABIL_DEFAULT_AREA).strip()
    if city == "يفرن":
        city = "غريان"
        area = "يفرن"
    destination = {
        "countryCode": _SABIL_COUNTRY_CODE,
        "city": city,
        **({"area": area} if area else {}),
        **({"address": address} if address else {}),
    }
    products = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        quantity = max(1, as_int(raw.get("quantity"), 1))
        products.append({
            "title": str(raw.get("name") or "منتج AVEA").strip(),
            "quantity": quantity,
            "widthCM": 10,
            "heightCM": 10,
            "lengthCM": 10,
            "allowInspection": True,
            "allowTesting": True,
            "isFragile": False,
            "amount": round(max(0.0, as_number(raw.get("price"), 0)), 2),
            "currency": _SABIL_CURRENCY,
            "isChargeable": True,
        })
    note = str(payload.get("note") or customer.get("note") or "").strip()
    return {
        "isPickup": False,
        "service": _SABIL_SERVICE_ID,
        "contacts": list(contact_ids if contact_ids is not None else _SABIL_CONTACT_IDS),
        "paymentBy": _SABIL_PAYMENT_BY if _SABIL_PAYMENT_BY in {"sender", "receiver", "sales"} else "receiver",
        "allowCardPayment": False,
        "allowSplitting": True,
        "allowedBankNotes": {"50": False},
        "to": destination,
        "products": products,
        **({"notes": note} if note else {}),
        "tags": [],
        "metadata": {},
    }


def _decode_jwt_expiry(token: str) -> float:
    return _decode_jwt_claim_number(token, "exp")


def _decode_jwt_claim_number(token: str, claim: str) -> float:
    try:
        payload = str(token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return float(decoded.get(claim) or 0)
    except Exception:
        return 0


def _load_sabil_session() -> None:
    global _SABIL_ACCESS_TOKEN, _SABIL_REFRESH_TOKEN
    try:
        stored = json.loads(_SABIL_SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(stored, dict):
        return
    stored_access = str(stored.get("accessToken") or "").strip()
    stored_refresh = str(stored.get("refreshToken") or "").strip()
    environment_issued_at = _decode_jwt_claim_number(_SABIL_ACCESS_TOKEN, "iat")
    stored_issued_at = _decode_jwt_claim_number(stored_access, "iat")
    use_stored = bool(stored_access and stored_refresh) and (
        not _SABIL_ACCESS_TOKEN
        or not _SABIL_REFRESH_TOKEN
        or stored_issued_at > environment_issued_at
    )
    if use_stored:
        _SABIL_ACCESS_TOKEN = stored_access
        _SABIL_REFRESH_TOKEN = stored_refresh


def _save_sabil_session() -> None:
    try:
        temporary = _SABIL_SESSION_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "accessToken": _SABIL_ACCESS_TOKEN,
            "refreshToken": _SABIL_REFRESH_TOKEN,
        }), encoding="utf-8")
        temporary.replace(_SABIL_SESSION_FILE)
    except Exception:
        pass


def _refresh_sabil_session(*, force: bool = False) -> None:
    global _SABIL_ACCESS_TOKEN, _SABIL_REFRESH_TOKEN
    if not _SABIL_REFRESH_TOKEN:
        raise RuntimeError("انتهت جلسة درب السبيل ويلزم تسجيل الدخول مجددًا")
    with _SABIL_SESSION_LOCK:
        expiry = _decode_jwt_expiry(_SABIL_ACCESS_TOKEN)
        if not force and expiry and expiry > time.time() + 60:
            return
        body = json.dumps({"refreshToken": _SABIL_REFRESH_TOKEN}).encode("utf-8")
        req = urllib.request.Request(
            f"{_SABIL_API_BASE_URL}/{_SABIL_REFRESH_PATH.lstrip('/')}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "X-API-VERSION": _SABIL_API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except Exception as ex:
            raise RuntimeError("تعذر تجديد جلسة درب السبيل") from ex
        authorization = decoded.get("data") if isinstance(decoded, dict) else None
        access = authorization.get("access") if isinstance(authorization, dict) else None
        refresh = authorization.get("refresh") if isinstance(authorization, dict) else None
        access_token = str(access.get("token") or "").strip() if isinstance(access, dict) else ""
        refresh_token = str(refresh.get("token") or "").strip() if isinstance(refresh, dict) else ""
        if not access_token:
            raise RuntimeError("لم يُرجع درب السبيل جلسة صالحة بعد التجديد")
        _SABIL_ACCESS_TOKEN = access_token
        if refresh_token:
            _SABIL_REFRESH_TOKEN = refresh_token
        _save_sabil_session()


_load_sabil_session()


def _sabil_headers() -> Dict[str, str]:
    if _SABIL_ACCESS_TOKEN:
        expiry = _decode_jwt_expiry(_SABIL_ACCESS_TOKEN)
        if expiry and expiry <= time.time() + 60:
            _refresh_sabil_session()
        authorization = f"Bearer {_SABIL_ACCESS_TOKEN}"
    else:
        authorization = f"apikey {_SABIL_API_KEY}"
    return {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ar",
        "Authorization": authorization,
        "Origin": "https://app.sabil.ly",
        "User-Agent": _SABIL_USER_AGENT,
        "Sec-CH-UA": '"Not/A)Brand";v="99", "Chromium";v="148"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "X-API-VERSION": _SABIL_API_VERSION,
        "X-ACCOUNT-ID": _SABIL_ACCOUNT_ID,
    }


def _request_sabil_api(
    path: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
) -> tuple[int, Any]:
    url = f"{_SABIL_API_BASE_URL}/{str(path or '').lstrip('/')}"
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    for attempt in range(2):
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=_sabil_headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                status_code = int(getattr(response, "status", 200))
                response_body = response.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as ex:
            error_body = ex.read().decode("utf-8", errors="replace").strip()
            if ex.code == 401 and attempt == 0 and _SABIL_ACCESS_TOKEN and _SABIL_REFRESH_TOKEN:
                _refresh_sabil_session(force=True)
                continue
            detail = ""
            if error_body:
                try:
                    decoded_error = json.loads(error_body)
                    detail = _first_nested_value(decoded_error, {"message", "error", "detail"})
                except Exception:
                    detail = error_body[:300]
            suffix = f" - {detail}" if detail else ""
            raise RuntimeError(f"Darb Al Sabeel HTTP {ex.code}: {ex.reason}{suffix}") from ex
        except urllib.error.URLError as ex:
            raise RuntimeError(f"تعذر الاتصال بدرب السبيل: {ex.reason}") from ex
    try:
        decoded = json.loads(response_body) if response_body.strip() else {}
    except Exception:
        decoded = {"raw": response_body[:500]}
    return status_code, decoded


def _normalized_libyan_phone(phone: Any) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("00218"):
        digits = digits[2:]
    if digits.startswith("218"):
        digits = f"0{digits[3:]}"
    return digits


def _sabil_contact_phone(phone: Any) -> str:
    raw = str(phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00218"):
        digits = digits[2:]
    if digits.startswith("218"):
        international = f"+{digits}"
    elif digits.startswith("0"):
        international = f"+218{digits[1:]}"
    elif raw.startswith("+"):
        international = f"+{digits}"
    elif len(digits) == 9 and digits.startswith("9"):
        international = f"+218{digits}"
    else:
        international = ""
    if not re.fullmatch(r"\+[1-9]\d{6,14}", international):
        raise RuntimeError("رقم هاتف العميل غير صالح للربط مع درب السبيل")
    return international


def _matching_sabil_contact_id(data: Any, phone: str) -> str:
    wanted = _normalized_libyan_phone(phone)
    if isinstance(data, dict):
        candidate_phone = _normalized_libyan_phone(data.get("phone"))
        if wanted and candidate_phone == wanted:
            contact_id = str(data.get("_id") or data.get("id") or "").strip()
            if contact_id:
                return contact_id
        for value in data.values():
            found = _matching_sabil_contact_id(value, phone)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _matching_sabil_contact_id(value, phone)
            if found:
                return found
    return ""


def _sabil_contact_for_order(order: Dict[str, Any]) -> str:
    if _SABIL_CONTACT_IDS:
        return _SABIL_CONTACT_IDS[0]
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    name = str(customer.get("name") or order.get("customerName") or "").strip()
    phone = str(customer.get("phone") or order.get("customerPhone") or "").strip()
    if not name or not phone:
        raise RuntimeError("اسم العميل ورقم الهاتف مطلوبان لإنشاء جهة اتصال درب السبيل")
    contact_phone = _sabil_contact_phone(phone)

    with _SABIL_CONTACT_LOCK:
        _, contacts = _request_sabil_api(f"{_SABIL_CONTACTS_PATH.rstrip('/')}/")
        existing_id = _matching_sabil_contact_id(contacts, phone)
        if existing_id:
            return existing_id

        _, created = _request_sabil_api(
            _SABIL_CONTACTS_PATH,
            method="POST",
            payload={"name": name, "phone": contact_phone},
        )
        contact_id = _first_nested_value(created, {"_id", "id", "contactid", "contact_id"})
        if not contact_id:
            raise RuntimeError("لم يُرجع درب السبيل معرف جهة الاتصال الجديدة")
        return contact_id


def _request_sabil_shipment(order: Dict[str, Any]) -> Dict[str, Any]:
    config = sabil_config_status()
    if not config["ready"]:
        missing = ", ".join(config["missing"])
        raise RuntimeError(f"إعدادات درب السبيل غير مكتملة: {missing or 'SABIL_ENABLED'}")
    contact_id = _sabil_contact_for_order(order)
    status_code, decoded = _request_sabil_api(
        _SABIL_CREATE_SHIPMENT_PATH,
        method="POST",
        payload=build_sabil_shipment_payload(order, [contact_id]),
    )
    shipment_id = _first_nested_value(decoded, {"id", "shipmentid", "shipment_id"})
    tracking_number = _first_nested_value(decoded, {"trackingnumber", "tracking_number", "tracking", "code", "number"})
    reference = _first_nested_value(decoded, {"reference", "referencecode", "reference_code"})
    return {
        "provider": "darb_sabeel",
        "status": "created",
        "shipmentId": shipment_id,
        "trackingNumber": tracking_number or shipment_id,
        "referenceCode": reference,
        "httpStatus": status_code,
        "lastError": "",
    }


def preview_sabil_shipping(order_id: str) -> Dict[str, Any]:
    order_id = str(order_id or "").strip()
    order = next(
        (
            normalize_order_item(row)
            for row in read_orders()
            if str(row.get("orderId") or "").strip() == order_id
        ),
        None,
    )
    if order is None:
        raise LookupError("Order not found")
    contact_id = _sabil_contact_for_order(order)
    status_code, decoded = _request_sabil_api(
        "/api/local/shipments/calculate/shipping",
        method="POST",
        payload=build_sabil_shipment_payload(order, [contact_id]),
    )
    return {"status": "ready", "httpStatus": status_code, "response": decoded}


def dispatch_order_to_sabil(order_id: str, force: bool = False) -> Dict[str, Any]:
    order_id = str(order_id or "").strip()
    now_ms = int(time.time() * 1000)
    with _INVENTORY_LOCK:
        entries = read_orders()
        idx = next((i for i, row in enumerate(entries) if str(row.get("orderId") or "").strip() == order_id), -1)
        if idx < 0:
            raise LookupError("Order not found")
        order = normalize_order_item(entries[idx])
        previous = order.get("externalDelivery") if isinstance(order.get("externalDelivery"), dict) else {}
        if not force and str(previous.get("status") or "") in {"created", "sending"}:
            return dict(previous)
        attempt = {
            **previous,
            "provider": "darb_sabeel",
            "status": "sending",
            "lastAttemptAtMs": now_ms,
            "lastError": "",
        }
        order["externalDelivery"] = attempt
        entries[idx] = order
        write_orders(entries)
    try:
        result = _request_sabil_shipment(order)
        result["createdAtMs"] = as_int(previous.get("createdAtMs"), now_ms)
    except Exception as ex:
        result = {**attempt, "status": "failed", "lastError": str(ex)[:700]}
    result["lastAttemptAtMs"] = now_ms
    with _INVENTORY_LOCK:
        entries = read_orders()
        idx = next((i for i, row in enumerate(entries) if str(row.get("orderId") or "").strip() == order_id), -1)
        if idx >= 0:
            updated = normalize_order_item(entries[idx])
            updated["externalDelivery"] = result
            updated["updatedAtMs"] = int(time.time() * 1000)
            entries[idx] = updated
            write_orders(entries)
    return result


def attach_sabil_shipment(order_id: str, shipment: Dict[str, Any]) -> Dict[str, Any]:
    order_id = str(order_id or "").strip()
    shipment_id = str(shipment.get("shipmentId") or "").strip()
    if not shipment_id:
        raise ValueError("shipmentId is required")
    now_ms = int(time.time() * 1000)
    with _INVENTORY_LOCK:
        entries = read_orders()
        idx = next((i for i, row in enumerate(entries) if str(row.get("orderId") or "").strip() == order_id), -1)
        if idx < 0:
            raise LookupError("Order not found")
        order = normalize_order_item(entries[idx])
        previous = order.get("externalDelivery") if isinstance(order.get("externalDelivery"), dict) else {}
        previous_id = str(previous.get("shipmentId") or "").strip()
        if previous_id and previous_id != shipment_id:
            raise RuntimeError("Order already has a different shipment")
        delivery = {
            **previous,
            "provider": "darb_sabeel",
            "status": "created",
            "shipmentId": shipment_id,
            "trackingNumber": str(shipment.get("trackingNumber") or shipment_id).strip(),
            "referenceCode": str(shipment.get("referenceCode") or "").strip(),
            "httpStatus": max(200, as_int(shipment.get("httpStatus"), 201)),
            "lastError": "",
            "createdAtMs": as_int(previous.get("createdAtMs"), now_ms),
            "lastAttemptAtMs": now_ms,
        }
        order["externalDelivery"] = delivery
        order["updatedAtMs"] = now_ms
        entries[idx] = order
        write_orders(entries)
    return delivery


def snapshot_order_purchase_costs(
    order: Dict[str, Any],
    products: List[Dict[str, Any]],
    current: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach authoritative unit purchase costs without changing old snapshots."""
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    lines = payload.get("items") if isinstance(payload.get("items"), list) else []
    current_payload = current.get("payload") if isinstance(current, dict) and isinstance(current.get("payload"), dict) else {}
    current_lines = current_payload.get("items") if isinstance(current_payload.get("items"), list) else []
    current_costs: Dict[str, float] = {}
    for raw in current_lines:
        if not isinstance(raw, dict):
            continue
        product_id = str(raw.get("productId") or raw.get("id") or "").strip()
        if product_id and raw.get("purchasePrice") is not None:
            current_costs[product_id] = max(0.0, as_number(raw.get("purchasePrice"), 0))
    product_costs = {
        str(product.get("id") or "").strip(): max(0.0, as_number(product.get("purchasePrice"), 0))
        for product in products
        if isinstance(product, dict) and str(product.get("id") or "").strip()
    }
    enriched = []
    for raw in lines:
        if not isinstance(raw, dict):
            continue
        line = dict(raw)
        product_id = str(line.get("productId") or line.get("id") or "").strip()
        line["purchasePrice"] = current_costs.get(product_id, product_costs.get(product_id, 0.0))
        enriched.append(line)
    payload = dict(payload)
    payload["items"] = enriched
    order = dict(order)
    order["payload"] = payload
    return order


def normalize_expense(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = current or {}
    now = int(time.time() * 1000)
    category = str(payload.get("category") or cur.get("category") or "other").strip().lower()
    if category not in {"rent", "shipping", "marketing", "salary", "utilities", "supplies", "other"}:
        category = "other"
    return {
        "id": str(payload.get("id") or cur.get("id") or f"exp_{now}_{uuid.uuid4().hex[:8]}").strip(),
        "amount": round(max(0.0, as_number(payload.get("amount", cur.get("amount", 0)), 0)), 2),
        "category": category,
        "description": str(payload.get("description") or cur.get("description") or "").strip(),
        "expenseAtMs": as_int(payload.get("expenseAtMs", cur.get("expenseAtMs", now)), now),
        "createdAtMs": as_int(payload.get("createdAtMs", cur.get("createdAtMs", now)), now),
        "updatedAtMs": now,
    }


def accounting_summary(from_ms: int = 0, to_ms: int = 0) -> Dict[str, Any]:
    products = read_products()
    product_by_id = {str(item.get("id") or "").strip(): item for item in products}
    delivered = []
    for raw in read_orders():
        if not isinstance(raw, dict):
            continue
        order = normalize_order_item(raw)
        created = as_int(order.get("createdAtMs"), 0)
        if str(order.get("status") or "").lower() != "delivered":
            continue
        if from_ms > 0 and created < from_ms:
            continue
        if to_ms > 0 and created > to_ms:
            continue
        delivered.append(order)

    revenue = cost_of_goods = ambassador_commissions = 0.0
    sold_pieces = missing_cost_pieces = 0
    product_sales: Dict[str, Dict[str, Any]] = {}
    for order in delivered:
        revenue += max(0.0, as_number(order.get("grandTotal"), 0))
        if bool(order.get("ambassadorSummary", {}).get("isAmbassadorOrder")):
            ambassador_commissions += _ambassador_order_commission(order)
        payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
        lines = payload.get("items") if isinstance(payload.get("items"), list) else []
        for raw_line in lines:
            if not isinstance(raw_line, dict):
                continue
            product_id = str(raw_line.get("productId") or raw_line.get("id") or "").strip()
            quantity = max(0, as_int(raw_line.get("quantity"), 0))
            sold_pieces += quantity
            purchase_price = as_number(raw_line.get("purchasePrice"), -1)
            if purchase_price < 0:
                purchase_price = as_number(product_by_id.get(product_id, {}).get("purchasePrice"), 0)
            if purchase_price <= 0:
                missing_cost_pieces += quantity
            cost_of_goods += max(0.0, purchase_price) * quantity
            sale_amount = max(0.0, as_number(raw_line.get("price"), 0)) * quantity
            row = product_sales.setdefault(product_id or str(raw_line.get("name") or "منتج"), {
                "productId": product_id,
                "name": str(raw_line.get("name") or product_by_id.get(product_id, {}).get("name") or "منتج"),
                "pieces": 0,
                "sales": 0.0,
                "cost": 0.0,
            })
            row["pieces"] += quantity
            row["sales"] += sale_amount
            row["cost"] += max(0.0, purchase_price) * quantity

    period_expenses = []
    for raw in read_expenses():
        expense = normalize_expense(raw, raw)
        expense_at = as_int(expense.get("expenseAtMs"), 0)
        if from_ms > 0 and expense_at < from_ms:
            continue
        if to_ms > 0 and expense_at > to_ms:
            continue
        period_expenses.append(expense)
    expenses_total = sum(max(0.0, as_number(item.get("amount"), 0)) for item in period_expenses)

    inventory_pieces = missing_cost_products = 0
    inventory_cost_value = inventory_sale_value = 0.0
    for product in products:
        quantity = max(0, as_int(product.get("availableStock", product.get("stockQuantity", 0)), 0))
        purchase_price = max(0.0, as_number(product.get("purchasePrice"), 0))
        sale_price = max(0.0, as_number(product.get("price"), 0))
        inventory_pieces += quantity
        inventory_cost_value += quantity * purchase_price
        inventory_sale_value += quantity * sale_price
        if quantity > 0 and purchase_price <= 0:
            missing_cost_products += 1

    gross_profit = revenue - cost_of_goods
    net_profit = gross_profit - ambassador_commissions - expenses_total
    top_products = sorted(product_sales.values(), key=lambda item: item["sales"], reverse=True)[:20]
    for item in top_products:
        item["sales"] = round(item["sales"], 2)
        item["cost"] = round(item["cost"], 2)
        item["profit"] = round(item["sales"] - item["cost"], 2)
    return {
        "fromMs": from_ms, "toMs": to_ms,
        "deliveredOrders": len(delivered), "soldPieces": sold_pieces,
        "revenue": round(revenue, 2), "costOfGoods": round(cost_of_goods, 2),
        "grossProfit": round(gross_profit, 2),
        "ambassadorCommissions": round(ambassador_commissions, 2),
        "expenses": round(expenses_total, 2), "netProfit": round(net_profit, 2),
        "inventoryPieces": inventory_pieces,
        "inventoryCostValue": round(inventory_cost_value, 2),
        "inventorySaleValue": round(inventory_sale_value, 2),
        "inventoryPotentialProfit": round(inventory_sale_value - inventory_cost_value, 2),
        "missingCostProducts": missing_cost_products,
        "missingCostSoldPieces": missing_cost_pieces,
        "topProducts": top_products,
    }


def _ambassador_order_owner_uid(order: Dict[str, Any]) -> str:
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    summary = order.get("ambassadorSummary") if isinstance(order.get("ambassadorSummary"), dict) else {}
    return str(summary.get("ambassadorUid") or customer.get("submitterUid") or order.get("uid") or "").strip()


def _ambassador_order_commission(order: Dict[str, Any]) -> float:
    summary = order.get("ambassadorSummary") if isinstance(order.get("ambassadorSummary"), dict) else {}
    explicit = as_number(summary.get("estimatedCommission", 0), 0)
    if explicit > 0:
        return round(explicit, 2)

    config = read_marketing_config().get("commission", {})
    default_percent = max(0.0, min(100.0, as_number(config.get("defaultPercent", 7), 7)))
    per_product = bool(config.get("perProductEnabled", True))
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    lines = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not lines:
        return round(max(0.0, as_number(order.get("grandTotal", 0), 0)) * default_percent / 100, 2)

    amount = 0.0
    for line in lines:
        if not isinstance(line, dict):
            continue
        product_percent = as_number(line.get("commissionPercent", 0), 0)
        percent = product_percent if per_product and product_percent > 0 else default_percent
        percent = max(0.0, min(100.0, percent))
        price = max(0.0, as_number(line.get("price", 0), 0))
        quantity = max(0, as_int(line.get("quantity", 0), 0))
        amount += price * quantity * percent / 100
    return round(amount, 2)


def ambassador_withdrawal_summary(uid: str) -> Dict[str, Any]:
    delivered_orders = [normalize_order_item(x) for x in read_orders() if isinstance(x, dict)]
    delivered_orders = [
        order for order in delivered_orders
        if str(order.get("status") or "").strip().lower() == "delivered"
        and _ambassador_order_owner_uid(order) == uid
    ]
    earned = round(sum(_ambassador_order_commission(order) for order in delivered_orders), 2)
    withdrawals = [
        dict(item) for item in read_ambassador_withdrawals()
        if str(item.get("ambassadorUid") or "").strip() == uid
    ]
    reserved = round(sum(
        max(0.0, as_number(item.get("amount", 0), 0))
        for item in withdrawals
        if str(item.get("status") or "pending").strip().lower() in {"pending", "approved", "paid"}
    ), 2)
    available = round(max(0.0, earned - reserved), 2)
    pending = next((
        item for item in sorted(withdrawals, key=lambda x: as_int(x.get("createdAtMs", 0), 0), reverse=True)
        if str(item.get("status") or "").strip().lower() in {"pending", "approved"}
    ), None)
    return {
        "minimum": AMBASSADOR_WITHDRAWAL_MINIMUM,
        "earned": earned,
        "reserved": reserved,
        "available": available,
        "remainingToMinimum": round(max(0.0, AMBASSADOR_WITHDRAWAL_MINIMUM - available), 2),
        "canRequest": available >= AMBASSADOR_WITHDRAWAL_MINIMUM and pending is None,
        "pendingRequest": pending,
        "requests": sorted(withdrawals, key=lambda x: as_int(x.get("createdAtMs", 0), 0), reverse=True),
    }


def _clean_inventory_option(value: Any) -> str:
    return re.sub(r'''^[\s\[\]"']+|[\s\[\]"']+$''', "", str(value or "").strip()).strip()


def _order_inventory_requests(order: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Optional[str]]:
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    totals: Dict[tuple[str, str], int] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        product_id = str(raw.get("productId") or raw.get("id") or "").strip()
        quantity = max(0, as_int(raw.get("quantity", 0), 0))
        size = _clean_inventory_option(raw.get("size"))
        if not product_id or quantity <= 0:
            continue
        key = (product_id, size)
        totals[key] = totals.get(key, 0) + quantity
    if not totals:
        return [], "الطلب لا يحتوي على منتجات صالحة"
    return [
        {"productId": product_id, "size": size, "quantity": quantity}
        for (product_id, size), quantity in totals.items()
    ], None


def _find_size_key(size_quantities: Dict[str, int], requested_size: str) -> Optional[str]:
    wanted = _clean_inventory_option(requested_size).casefold()
    for key in size_quantities:
        if _clean_inventory_option(key).casefold() == wanted:
            return key
    return None


def _refresh_product_inventory(product: Dict[str, Any]) -> None:
    size_quantities = normalize_quantity_map(product.get("sizeQuantities", {}))
    if size_quantities:
        available = _quantity_map_total(size_quantities)
        product["sizeQuantities"] = size_quantities
        product["stockQuantity"] = available
    else:
        available = max(0, as_int(product.get("stockQuantity", 0), 0))
        product["stockQuantity"] = available
    threshold = max(0, as_int(product.get("lowStockThreshold", 0), 0))
    product["availableStock"] = available
    product["outOfStock"] = 1 if available <= 0 else 0
    product["lowStock"] = 1 if 0 < available <= threshold else 0
    product["lowStockSizes"] = [
        key for key, quantity in size_quantities.items()
        if threshold > 0 and quantity <= threshold
    ]
    product["updatedAt"] = int(time.time() * 1000)


def reserve_order_inventory(products: List[Dict[str, Any]], order: Dict[str, Any]) -> tuple[bool, str, List[Dict[str, Any]]]:
    requests, error = _order_inventory_requests(order)
    if error:
        return False, error, []
    by_id = {str(product.get("id") or "").strip(): product for product in products}
    movements: List[Dict[str, Any]] = []

    for request_item in requests:
        product = by_id.get(request_item["productId"])
        if product is None:
            return False, "أحد المنتجات لم يعد متوفرًا", []
        quantity = request_item["quantity"]
        size_quantities = normalize_quantity_map(product.get("sizeQuantities", {}))
        if size_quantities:
            if not request_item["size"]:
                return False, f"يرجى تحديد مقاس {product.get('name') or 'المنتج'}", []
            stored_key = _find_size_key(size_quantities, request_item["size"])
            available = size_quantities.get(stored_key, 0) if stored_key is not None else 0
            if stored_key is None or available < quantity:
                return False, f"المتوفر من مقاس {request_item['size']} للمنتج {product.get('name') or ''} هو {available} فقط", []
            movements.append({**request_item, "storedSizeKey": stored_key})
        else:
            available = max(0, as_int(product.get("stockQuantity", product.get("availableStock", 0)), 0))
            if available < quantity:
                return False, f"المتوفر من المنتج {product.get('name') or ''} هو {available} فقط", []
            movements.append(dict(request_item))

    for movement in movements:
        product = by_id[movement["productId"]]
        size_quantities = normalize_quantity_map(product.get("sizeQuantities", {}))
        if size_quantities:
            key = movement["storedSizeKey"]
            size_quantities[key] -= movement["quantity"]
            product["sizeQuantities"] = size_quantities
        else:
            product["stockQuantity"] = max(0, as_int(product.get("stockQuantity", 0), 0) - movement["quantity"])
        _refresh_product_inventory(product)
    return True, "", movements


def restore_order_inventory(products: List[Dict[str, Any]], reservation: List[Dict[str, Any]]) -> None:
    by_id = {str(product.get("id") or "").strip(): product for product in products}
    for movement in reservation:
        if not isinstance(movement, dict):
            continue
        product = by_id.get(str(movement.get("productId") or "").strip())
        quantity = max(0, as_int(movement.get("quantity", 0), 0))
        if product is None or quantity <= 0:
            continue
        size_quantities = normalize_quantity_map(product.get("sizeQuantities", {}))
        if size_quantities:
            key = str(movement.get("storedSizeKey") or "").strip()
            if key not in size_quantities:
                key = _find_size_key(size_quantities, str(movement.get("size") or "")) or key
            if key:
                size_quantities[key] = max(0, as_int(size_quantities.get(key, 0), 0)) + quantity
                product["sizeQuantities"] = size_quantities
        else:
            product["stockQuantity"] = max(0, as_int(product.get("stockQuantity", 0), 0)) + quantity
        _refresh_product_inventory(product)


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
        "accountRole": str(payload.get("accountRole") or cur.get("accountRole") or "customer").strip().lower(),
        "isAmbassador": bool(payload.get("isAmbassador", cur.get("isAmbassador", False))),
        "ambassadorName": str(payload.get("ambassadorName") or cur.get("ambassadorName") or "").strip(),
        "ambassadorPhone": str(payload.get("ambassadorPhone") or cur.get("ambassadorPhone") or "").strip(),
        "ambassadorAddress": str(payload.get("ambassadorAddress") or cur.get("ambassadorAddress") or "").strip(),
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
    image_url = str(payload.get("imageUrl") or cur.get("imageUrl") or "").strip()
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
        "imageUrl": image_url,
        "audience": audience,
        "uid": uid,
        "createdAtMs": as_int(payload.get("createdAtMs", cur.get("createdAtMs", now_ms)), now_ms),
    }


def _status_label_ar(status: str) -> str:
    s = str(status or "").strip().lower()
    if s == "pending":
        return "قيد الانتظار"
    if s == "processing":
        return "قيد المعالجة"
    if s == "shipped":
        return "تم الشحن"
    if s == "delivered":
        return "تم التوصيل"
    if s == "canceled":
        return "ملغي"
    return "محدث"


def _extract_order_image_url(order_item: Dict[str, Any]) -> str:
    payload = order_item.get("payload") if isinstance(order_item.get("payload"), dict) else {}
    if not isinstance(payload, dict):
        return ""
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    for x in items:
        if not isinstance(x, dict):
            continue
        u = str(x.get("imageUrl") or "").strip()
        if u:
            return u
    return ""


def _notify_user_on_order_status_change(order_item: Dict[str, Any], *, old_status: str, new_status: str) -> None:
    if str(old_status or "").strip().lower() == str(new_status or "").strip().lower():
        return

    uid = str(order_item.get("uid") or "").strip()
    payload = order_item.get("payload") if isinstance(order_item.get("payload"), dict) else {}
    if not uid and isinstance(payload, dict):
        customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
        uid = str((customer or {}).get("submitterUid") or "").strip()
    if not uid:
        return

    now_ms = int(time.time() * 1000)
    order_id = str(order_item.get("orderId") or "").strip()
    title = f"تحديث حالة الطلب #{order_id}" if order_id else "تحديث حالة الطلب"
    body = f"تم تحديث حالة طلبك إلى: {_status_label_ar(new_status)}"
    image_url = _extract_order_image_url(order_item)

    db, _ = _firestore_db()
    if db:
        try:
            doc_id = f"n_order_{now_ms}_{uuid.uuid4().hex[:10]}"
            ref = db.collection("users").document(uid).collection("notifications").document(doc_id)
            ref.set({
                "title": title,
                "body": body,
                "target": "orders",
                "targetId": order_id,
                "imageUrl": image_url,
                "read": False,
                "createdAtMs": now_ms,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "updatedAt": firestore.SERVER_TIMESTAMP,
                "source": "order_status",
            }, merge=True)
        except Exception:
            pass

    try:
        entries = read_notifications()
        entries.append(normalize_notification_item({
            "title": title,
            "body": body,
            "target": "orders",
            "targetId": order_id,
            "imageUrl": image_url,
            "audience": "user",
            "uid": uid,
            "createdAtMs": now_ms,
        }))
        entries.sort(key=lambda x: as_int(x.get("createdAtMs", 0)), reverse=True)
        entries = entries[:2000]
        write_notifications(entries)
    except Exception:
        pass


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


def normalize_look_item(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cur = current or {}
    now_ms = int(time.time() * 1000)
    look_id = str(payload.get("id") or cur.get("id") or f"look_{now_ms}_{uuid.uuid4().hex[:8]}").strip()
    title = str(payload.get("title") or cur.get("title") or "").strip()
    subtitle = str(payload.get("subtitle") or cur.get("subtitle") or "").strip()
    description = str(payload.get("description") or cur.get("description") or "").strip()
    badge = str(payload.get("badge") or cur.get("badge") or "إطلالة كاملة").strip()
    cta_label = str(payload.get("ctaLabel") or cur.get("ctaLabel") or "تسوقي الإطلالة").strip()
    cover = str(
        payload.get("coverImageUrl")
        or cur.get("coverImageUrl")
        or payload.get("imageUrl")
        or cur.get("imageUrl")
        or ""
    ).strip()
    product_ids = normalize_string_list(payload.get("productIds", cur.get("productIds", [])))
    image_urls = normalize_image_urls(payload.get("imageUrls", cur.get("imageUrls", [])))
    if cover and cover not in image_urls:
        image_urls = [cover, *image_urls]
    if not cover and image_urls:
        cover = image_urls[0]

    discount_percent = as_number(payload.get("discountPercent", cur.get("discountPercent", 0)), 0.0)
    discount_percent = max(0.0, min(100.0, discount_percent))
    is_hidden = as_hidden_int(payload.get("isHidden", cur.get("isHidden", 0)))
    sort_order = as_int(payload.get("sortOrder", cur.get("sortOrder", now_ms)), now_ms)
    created_at = as_int(payload.get("createdAt", cur.get("createdAt", now_ms)), now_ms)

    return {
        "id": look_id,
        "title": title,
        "subtitle": subtitle,
        "description": description,
        "badge": badge,
        "ctaLabel": cta_label,
        "coverImageUrl": cover,
        "imageUrl": cover,
        "imageUrls": image_urls,
        "productIds": product_ids,
        "discountPercent": discount_percent,
        "isHidden": is_hidden,
        "sortOrder": sort_order,
        "createdAt": created_at,
        "updatedAt": now_ms,
    }


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


def normalize_product_code_text(value: Any) -> str:
    raw = str(value or "").strip().upper()
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"[^A-Z0-9\-_]", "", raw)
    raw = re.sub(r"-{2,}", "-", raw)
    return raw.strip("-")


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
    product_code = normalize_product_code_text(payload.get("productCode") or cur.get("productCode") or "")
    if not product_code:
        product_code = generate_product_code(created_at=created_at)

    image_url = str(payload.get("imageUrl") or cur.get("imageUrl") or "").strip()
    image_urls = normalize_image_urls(payload.get("imageUrls", cur.get("imageUrls", [])))
    image_urls = list(dict.fromkeys(image_urls))
    if image_url:
        image_urls = [image_url, *[url for url in image_urls if url != image_url]]
    colors = normalize_string_list(payload.get("colors", cur.get("colors", [])))
    sizes = normalize_string_list(payload.get("sizes", cur.get("sizes", [])))
    size_type = str(payload.get("sizeType") or cur.get("sizeType") or "clothing").strip()
    if size_type not in {"clothing", "abaya", "shoes", "oneSize"}:
        size_type = "clothing"
    size_quantities = normalize_quantity_map(payload.get("sizeQuantities", cur.get("sizeQuantities", {})))
    if sizes:
        size_quantities = {size: max(0, as_int(size_quantities.get(size, 0), 0)) for size in sizes}
    color_quantities = normalize_quantity_map(payload.get("colorQuantities", cur.get("colorQuantities", {})))
    stock_quantity = max(0, as_int(payload.get("stockQuantity", cur.get("stockQuantity", 0)), 0))
    low_stock_threshold = max(0, as_int(payload.get("lowStockThreshold", cur.get("lowStockThreshold", 0)), 0))

    if not image_url and image_urls:
        image_url = image_urls[0]

    size_total = _quantity_map_total(size_quantities)
    if size_quantities:
        stock_quantity = size_total

    available_stock = stock_quantity
    if available_stock <= 0:
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
        "purchasePrice": max(0.0, as_number(payload.get("purchasePrice", cur.get("purchasePrice", 0)))),
        "commissionPercent": max(0, min(100, as_number(payload.get("commissionPercent", cur.get("commissionPercent", 0))))),
        "imageUrl": image_url,
        "imageUrls": image_urls,
        "description": description,
        "category": category,
        "tags": tags,
        "rating": as_number(payload.get("rating", cur.get("rating", 0))),
        "reviewsCount": max(0, as_int(payload.get("reviewsCount", cur.get("reviewsCount", 0)))),
        "isHidden": as_hidden_int(payload.get("isHidden", cur.get("isHidden", 0))),
        "sizes": ",".join(sizes),
        "sizeType": size_type,
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
    response = send_from_directory(ROOT, "admin_panel_v2.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
        "features": {
            "perSizeInventoryReservation": True,
            "restoreInventoryOnCancellation": True,
        },
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
            "accountRole": str(d.get("accountRole") or "customer"),
            "isAmbassador": bool(d.get("isAmbassador", False)),
            "ambassadorName": str(d.get("ambassadorName") or ""),
            "ambassadorPhone": str(d.get("ambassadorPhone") or ""),
            "ambassadorAddress": str(d.get("ambassadorAddress") or ""),
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
    looks = read_looks()
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
            "completeLooks": len(looks),
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


@app.get("/looks")
def list_complete_looks():
    include_hidden = str(request.args.get("includeHidden", "")).strip() == "1"
    looks = read_looks()
    if not include_hidden:
        looks = [x for x in looks if as_hidden_int(x.get("isHidden", 0)) == 0]

    product_map = {str(p.get("id") or "").strip(): p for p in read_products()}
    items = []
    for look in looks:
        row = dict(look)
        row["products"] = [
            product_map[pid]
            for pid in row.get("productIds", [])
            if isinstance(pid, str) and pid in product_map
        ]
        row["productsCount"] = len(row["products"])
        items.append(row)

    items.sort(key=lambda p: as_int(p.get("sortOrder", p.get("createdAt", 0)), 0), reverse=True)
    return jsonify({"ok": True, "count": len(items), "items": items})


@app.post("/looks")
def add_complete_look():
    ok, err = require_admin()
    if not ok:
        return err

    payload = request.get_json(silent=True) or {}
    looks = read_looks()
    item = normalize_look_item(payload)
    if not item["title"]:
        return jsonify({"ok": False, "error": "title is required"}), 400
    if not item["productIds"]:
        return jsonify({"ok": False, "error": "productIds is required"}), 400
    if any(str(x.get("id", "")).strip() == item["id"] for x in looks):
        return jsonify({"ok": False, "error": "Look id already exists"}), 409

    looks.append(item)
    write_looks(looks)
    return jsonify({"ok": True, "item": item}), 201


@app.put("/looks/<look_id>")
def update_complete_look(look_id: str):
    ok, err = require_admin()
    if not ok:
        return err

    payload = request.get_json(silent=True) or {}
    look_id = str(look_id or "").strip()
    looks = read_looks()
    idx = next((i for i, x in enumerate(looks) if str(x.get("id", "")).strip() == look_id), -1)
    if idx < 0:
        return jsonify({"ok": False, "error": "Look not found"}), 404

    merged = dict(looks[idx])
    merged.update(payload)
    merged["id"] = look_id
    item = normalize_look_item(merged, looks[idx])
    if not item["title"]:
        return jsonify({"ok": False, "error": "title is required"}), 400
    if not item["productIds"]:
        return jsonify({"ok": False, "error": "productIds is required"}), 400

    looks[idx] = item
    write_looks(looks)
    return jsonify({"ok": True, "item": item})


@app.delete("/looks/<look_id>")
def delete_complete_look(look_id: str):
    ok, err = require_admin()
    if not ok:
        return err

    look_id = str(look_id or "").strip()
    looks = read_looks()
    idx = next((i for i, x in enumerate(looks) if str(x.get("id", "")).strip() == look_id), -1)
    if idx < 0:
        return jsonify({"ok": False, "error": "Look not found"}), 404

    deleted = looks.pop(idx)
    write_looks(looks)
    return jsonify({"ok": True, "deleted": deleted})


@app.post("/orders")
def create_order_from_app():
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("orderId") or "").strip()
    order_payload = payload.get("payload")
    if not order_id:
        return jsonify({"ok": False, "error": "orderId is required"}), 400
    if not isinstance(order_payload, dict):
        return jsonify({"ok": False, "error": "payload is required"}), 400

    auth_header = str(request.headers.get("Authorization", "") or "").strip()
    if auth_header.startswith("Bearer "):
        signed_user, auth_error = _firebase_user_from_request()
        if auth_error is not None:
            return auth_error
        user_uid = str(signed_user.get("uid") or "").strip()
        profile = _firebase_user_profile(user_uid)
        customer = order_payload.get("customer") if isinstance(order_payload.get("customer"), dict) else {}
        customer = dict(customer)
        is_ambassador = str(profile.get("accountRole") or "").strip().lower() == "ambassador"
        customer.update({
            "submitterUid": user_uid,
            "submitterEmail": str(signed_user.get("email") or "").strip(),
            "submitterName": str(profile.get("ambassadorName") or profile.get("name") or signed_user.get("name") or "").strip(),
            "submitterPhone": str(profile.get("ambassadorPhone") or profile.get("phone") or "").strip(),
            "accountRole": "ambassador" if is_ambassador else "customer",
            "placedAsAmbassador": is_ambassador,
            "identityVerified": True,
        })
        order_payload = dict(order_payload)
        order_payload["customer"] = customer
        payload = dict(payload)
        payload["payload"] = order_payload
        payload["uid"] = user_uid

    with _INVENTORY_LOCK:
        entries = read_orders()
        idx = next((i for i, o in enumerate(entries) if str(o.get("orderId", "")).strip() == order_id), -1)

        item_payload = dict(payload)
        item_payload["orderId"] = order_id
        item_payload["source"] = "app"
        item_payload["updatedAtMs"] = int(time.time() * 1000)

        created = idx < 0
        products = read_products()
        if created:
            item = normalize_order_item(item_payload)
            item = snapshot_order_purchase_costs(item, products)
            reserved, inventory_error, movements = reserve_order_inventory(products, item)
            if not reserved:
                return jsonify({"ok": False, "error": inventory_error, "code": "insufficient_stock"}), 409
            item["inventoryReserved"] = True
            item["inventoryReservation"] = movements
            write_products(products)
            entries.append(item)
        else:
            item = normalize_order_item(item_payload, entries[idx])
            item = snapshot_order_purchase_costs(item, products, entries[idx])
            entries[idx] = item

        entries.sort(key=lambda x: as_int(x.get("createdAtMs", 0), 0), reverse=True)
        entries = entries[:5000]
        write_orders(entries)

    delivery = item.get("externalDelivery", {})
    if created and _SABIL_ENABLED:
        delivery = dispatch_order_to_sabil(order_id)
    return jsonify({
        "ok": True,
        "created": created,
        "orderId": order_id,
        "status": item.get("status", "pending"),
        "externalDelivery": delivery,
    })


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


@app.get("/admin/delivery/darb-sabeel/status")
def admin_sabil_status():
    ok, err = require_admin()
    if not ok:
        return err
    return jsonify({"ok": True, "config": sabil_config_status()})


@app.post("/orders/<order_id>/delivery/darb-sabeel")
def admin_send_order_to_sabil(order_id: str):
    ok, err = require_admin()
    if not ok:
        return err
    config = sabil_config_status()
    if not config["ready"]:
        return jsonify({
            "ok": False,
            "error": "أكملي بيانات حساب درب السبيل في إعدادات خادم Render",
            "config": config,
        }), 503
    force = bool((request.get_json(silent=True) or {}).get("force", False))
    try:
        delivery = dispatch_order_to_sabil(order_id, force=force)
    except LookupError:
        return jsonify({"ok": False, "error": "Order not found"}), 404
    if delivery.get("status") != "created":
        return jsonify({"ok": False, "error": delivery.get("lastError") or "تعذر إنشاء الشحنة", "delivery": delivery}), 502
    order = next((normalize_order_item(row) for row in read_orders() if str(row.get("orderId") or "").strip() == order_id), None)
    return jsonify({"ok": True, "delivery": delivery, "item": order})


@app.post("/orders/<order_id>/delivery/darb-sabeel/preview")
def admin_preview_order_sabil_shipping(order_id: str):
    ok, err = require_admin()
    if not ok:
        return err
    try:
        preview = preview_sabil_shipping(order_id)
    except LookupError:
        return jsonify({"ok": False, "error": "Order not found"}), 404
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)[:700]}), 502
    return jsonify({"ok": True, "preview": preview})


@app.post("/orders/<order_id>/delivery/darb-sabeel/attach")
def admin_attach_sabil_shipment(order_id: str):
    ok, err = require_admin()
    if not ok:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        delivery = attach_sabil_shipment(order_id, payload)
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    except LookupError:
        return jsonify({"ok": False, "error": "Order not found"}), 404
    except RuntimeError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 409
    order = next((normalize_order_item(row) for row in read_orders() if str(row.get("orderId") or "").strip() == order_id), None)
    return jsonify({"ok": True, "delivery": delivery, "item": order})


@app.get("/orders/statuses")
def list_order_statuses_for_app():
    limit = as_int(request.args.get("limit", 1000), 1000)
    limit = max(1, min(limit, 5000))
    since_ms = as_int(request.args.get("sinceMs", 0), 0)
    uid = str(request.args.get("uid", "") or "").strip()

    items = [normalize_order_item(x) for x in read_orders() if isinstance(x, dict)]
    if uid:
        items = [x for x in items if str(x.get("uid") or "").strip() == uid]

    compact = []
    for x in items:
        updated = as_int(x.get("updatedAtMs", x.get("createdAtMs", 0)), 0)
        if updated <= since_ms:
            continue
        compact.append({
            "orderId": str(x.get("orderId") or "").strip(),
            "status": str(x.get("status") or "pending").strip().lower(),
            "updatedAtMs": updated,
        })

    compact = [x for x in compact if x["orderId"]]
    compact.sort(key=lambda x: as_int(x.get("updatedAtMs", 0), 0), reverse=True)
    compact = compact[:limit]
    return jsonify({"ok": True, "count": len(compact), "items": compact})


@app.get("/orders/feed")
def list_orders_feed_for_app():
    limit = as_int(request.args.get("limit", 200), 200)
    limit = max(1, min(limit, 1000))
    uid = str(request.args.get("uid", "") or "").strip()
    if not uid:
        return jsonify({"ok": True, "count": 0, "items": []})

    items = [normalize_order_item(x) for x in read_orders() if isinstance(x, dict)]

    def belongs_to_uid(order: Dict[str, Any]) -> bool:
        direct_uid = str(order.get("uid") or "").strip()
        if direct_uid == uid:
            return True
        payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
        customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
        submitter_uid = str(customer.get("submitterUid") or "").strip()
        return submitter_uid == uid

    out = []
    for x in items:
        if not belongs_to_uid(x):
            continue
        payload = x.get("payload") if isinstance(x.get("payload"), dict) else {}
        ambassador_summary = x.get("ambassadorSummary") if isinstance(x.get("ambassadorSummary"), dict) else payload.get("ambassadorSummary")
        if not isinstance(ambassador_summary, dict):
            ambassador_summary = {}
        out.append({
            "orderId": str(x.get("orderId") or "").strip(),
            "status": str(x.get("status") or "pending").strip().lower(),
            "createdAtMs": as_int(x.get("createdAtMs", 0), 0),
            "updatedAtMs": as_int(x.get("updatedAtMs", x.get("createdAtMs", 0)), 0),
            "payload": payload,
            "ambassadorSummary": ambassador_summary,
            "uid": str(x.get("uid") or "").strip(),
        })

    out = [x for x in out if x["orderId"]]
    out.sort(key=lambda x: as_int(x.get("createdAtMs", 0), 0), reverse=True)
    out = out[:limit]
    return jsonify({"ok": True, "count": len(out), "items": out})


@app.get("/ambassadors/me/orders")
def list_current_ambassador_orders():
    signed_user, auth_error = _firebase_user_from_request()
    if auth_error is not None:
        return auth_error
    uid = str(signed_user.get("uid") or "").strip()
    profile = _firebase_user_profile(uid)
    if str(profile.get("accountRole") or "").strip().lower() != "ambassador":
        return jsonify({"ok": False, "error": "أكملي تسجيل بيانات المندوبة أولًا"}), 403

    limit = max(1, min(as_int(request.args.get("limit", 500), 500), 1000))
    items = [normalize_order_item(x) for x in read_orders() if isinstance(x, dict)]
    out = []
    for item in items:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
        summary = item.get("ambassadorSummary") if isinstance(item.get("ambassadorSummary"), dict) else {}
        owner_uid = str(summary.get("ambassadorUid") or customer.get("submitterUid") or item.get("uid") or "").strip()
        if owner_uid != uid:
            continue
        out.append({
            "orderId": str(item.get("orderId") or "").strip(),
            "status": str(item.get("status") or "pending").strip().lower(),
            "createdAtMs": as_int(item.get("createdAtMs", 0), 0),
            "updatedAtMs": as_int(item.get("updatedAtMs", 0), 0),
            "customerName": str(item.get("customerName") or "").strip(),
            "customerPhone": str(item.get("customerPhone") or "").strip(),
            "customerAddress": str(item.get("customerAddress") or "").strip(),
            "customerCity": str(item.get("city") or "").strip(),
            "grandTotal": as_number(item.get("grandTotal", 0), 0),
            "itemsCount": as_int(item.get("itemsCount", 0), 0),
            "payload": payload,
            "ambassadorSummary": summary,
        })
    out.sort(key=lambda x: as_int(x.get("createdAtMs", 0), 0), reverse=True)
    return jsonify({"ok": True, "count": len(out[:limit]), "items": out[:limit]})


@app.get("/ambassadors/me/profile")
def get_current_ambassador_profile():
    signed_user, auth_error = _firebase_user_from_request()
    if auth_error is not None:
        return auth_error
    uid = str(signed_user.get("uid") or "").strip()
    profile = _firebase_user_profile(uid)
    if str(profile.get("accountRole") or "").strip().lower() != "ambassador":
        return jsonify({"ok": True, "profile": None})
    return jsonify({"ok": True, "profile": profile})


@app.get("/ambassadors/me/withdrawals")
def get_current_ambassador_withdrawals():
    signed_user, auth_error = _firebase_user_from_request()
    if auth_error is not None:
        return auth_error
    uid = str(signed_user.get("uid") or "").strip()
    profile = _firebase_user_profile(uid)
    if str(profile.get("accountRole") or "").strip().lower() != "ambassador":
        return jsonify({"ok": False, "error": "حساب مندوبة مفعّل مطلوب"}), 403
    return jsonify({"ok": True, **ambassador_withdrawal_summary(uid)})


@app.post("/ambassadors/me/withdrawals")
def create_current_ambassador_withdrawal():
    signed_user, auth_error = _firebase_user_from_request()
    if auth_error is not None:
        return auth_error
    uid = str(signed_user.get("uid") or "").strip()
    profile = _firebase_user_profile(uid)
    if str(profile.get("accountRole") or "").strip().lower() != "ambassador":
        return jsonify({"ok": False, "error": "حساب مندوبة مفعّل مطلوب"}), 403

    with _WITHDRAWAL_LOCK:
        summary = ambassador_withdrawal_summary(uid)
        if summary.get("pendingRequest"):
            return jsonify({
                "ok": False,
                "code": "withdrawal_pending",
                "error": "لديك طلب سحب قيد المراجعة بالفعل",
                **summary,
            }), 409
        available = as_number(summary.get("available", 0), 0)
        if available < AMBASSADOR_WITHDRAWAL_MINIMUM:
            return jsonify({
                "ok": False,
                "code": "minimum_not_reached",
                "error": f"يمكن طلب السحب بعد وصول الأرباح المعتمدة إلى {AMBASSADOR_WITHDRAWAL_MINIMUM:.0f} د.ل",
                **summary,
            }), 409

        now_ms = int(time.time() * 1000)
        item = {
            "id": f"wd_{now_ms}_{uuid.uuid4().hex[:8]}",
            "ambassadorUid": uid,
            "ambassadorName": str(profile.get("ambassadorName") or profile.get("name") or "").strip(),
            "ambassadorPhone": str(profile.get("ambassadorPhone") or profile.get("phone") or "").strip(),
            "amount": round(available, 2),
            "status": "pending",
            "createdAtMs": now_ms,
            "updatedAtMs": now_ms,
        }
        entries = read_ambassador_withdrawals()
        entries.append(item)
        entries.sort(key=lambda x: as_int(x.get("createdAtMs", 0), 0), reverse=True)
        write_ambassador_withdrawals(entries[:5000])
        updated_summary = ambassador_withdrawal_summary(uid)
    return jsonify({"ok": True, "request": item, **updated_summary}), 201


@app.get("/admin/ambassador-withdrawals")
def list_admin_ambassador_withdrawals():
    ok, err = require_admin()
    if not ok:
        return err
    entries = sorted(
        read_ambassador_withdrawals(),
        key=lambda x: as_int(x.get("createdAtMs", 0), 0),
        reverse=True,
    )
    return jsonify({"ok": True, "count": len(entries), "items": entries})


@app.get("/admin/accounting/summary")
def get_admin_accounting_summary():
    ok, err = require_admin()
    if not ok:
        return err
    from_ms = max(0, as_int(request.args.get("fromMs", 0), 0))
    to_ms = max(0, as_int(request.args.get("toMs", 0), 0))
    return jsonify({"ok": True, "summary": accounting_summary(from_ms, to_ms)})


@app.get("/admin/expenses")
def list_admin_expenses():
    ok, err = require_admin()
    if not ok:
        return err
    items = [normalize_expense(item, item) for item in read_expenses() if isinstance(item, dict)]
    items.sort(key=lambda item: as_int(item.get("expenseAtMs"), 0), reverse=True)
    return jsonify({"ok": True, "count": len(items), "items": items[:1000]})


@app.post("/admin/expenses")
def create_admin_expense():
    ok, err = require_admin()
    if not ok:
        return err
    payload = request.get_json(silent=True) or {}
    item = normalize_expense(payload)
    if item["amount"] <= 0 or not item["description"]:
        return jsonify({"ok": False, "error": "المبلغ والوصف مطلوبان"}), 400
    with _EXPENSES_LOCK:
        items = read_expenses()
        items.append(item)
        items.sort(key=lambda row: as_int(row.get("expenseAtMs"), 0), reverse=True)
        write_expenses(items[:5000])
    return jsonify({"ok": True, "item": item}), 201


@app.delete("/admin/expenses/<expense_id>")
def delete_admin_expense(expense_id: str):
    ok, err = require_admin()
    if not ok:
        return err
    with _EXPENSES_LOCK:
        items = read_expenses()
        idx = next((i for i, item in enumerate(items) if str(item.get("id") or "") == expense_id), -1)
        if idx < 0:
            return jsonify({"ok": False, "error": "المصروف غير موجود"}), 404
        deleted = items.pop(idx)
        write_expenses(items)
    return jsonify({"ok": True, "deleted": deleted})


@app.put("/admin/ambassador-withdrawals/<withdrawal_id>/status")
def update_admin_ambassador_withdrawal_status(withdrawal_id: str):
    ok, err = require_admin()
    if not ok:
        return err
    next_status = str((request.get_json(silent=True) or {}).get("status") or "").strip().lower()
    allowed_transitions = {
        "pending": {"approved", "rejected"},
        "approved": {"paid", "rejected"},
        "paid": set(),
        "rejected": set(),
    }
    with _WITHDRAWAL_LOCK:
        entries = read_ambassador_withdrawals()
        idx = next((i for i, item in enumerate(entries) if str(item.get("id") or "") == withdrawal_id), -1)
        if idx < 0:
            return jsonify({"ok": False, "error": "طلب السحب غير موجود"}), 404
        current_status = str(entries[idx].get("status") or "pending").strip().lower()
        if next_status not in allowed_transitions.get(current_status, set()):
            return jsonify({"ok": False, "error": "لا يمكن تغيير طلب السحب إلى هذه الحالة"}), 409
        entries[idx] = {
            **entries[idx],
            "status": next_status,
            "updatedAtMs": int(time.time() * 1000),
        }
        write_ambassador_withdrawals(entries)
        item = entries[idx]
    return jsonify({"ok": True, "item": item})


@app.get("/admin/ambassadors")
def list_admin_ambassadors():
    ok, err = require_admin()
    if not ok:
        return err
    items, profiles_error = _firebase_ambassador_profiles()
    return jsonify({
        "ok": True,
        "count": len(items),
        "items": items,
        "source": "firestore" if not profiles_error else "unavailable",
        "warning": profiles_error,
    })


@app.put("/ambassadors/me/profile")
def save_current_ambassador_profile():
    signed_user, auth_error = _firebase_user_from_request()
    if auth_error is not None:
        return auth_error
    uid = str(signed_user.get("uid") or "").strip()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("ambassadorName") or "").strip()
    phone = re.sub(r"[^0-9+]", "", str(payload.get("ambassadorPhone") or "").strip())
    address = str(payload.get("ambassadorAddress") or "").strip()
    if len(name) < 2:
        return jsonify({"ok": False, "error": "أدخلي الاسم الكامل"}), 400
    if not re.fullmatch(r"\+?[0-9]{8,15}", phone):
        return jsonify({"ok": False, "error": "رقم الهاتف غير صحيح"}), 400
    if len(address) < 4:
        return jsonify({"ok": False, "error": "أدخلي المدينة والمنطقة"}), 400

    now = int(time.time() * 1000)
    existing = _firebase_user_profile(uid)
    profile = {
        "uid": uid,
        "accountRole": "ambassador",
        "ambassadorName": name,
        "ambassadorPhone": phone,
        "ambassadorAddress": address,
        "email": str(signed_user.get("email") or existing.get("email") or "").strip(),
        "status": "active",
        "joinedAt": as_int(existing.get("joinedAt"), now),
        "updatedAt": now,
    }
    saved, save_error = _save_firebase_user_profile(uid, profile)
    if not saved:
        return jsonify({"ok": False, "error": "تعذر حفظ حساب المندوبة", "details": save_error}), 503
    return jsonify({"ok": True, "profile": profile})


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
    with _INVENTORY_LOCK:
        entries = read_orders()
        idx = next((i for i, o in enumerate(entries) if str(o.get("orderId", "")).strip() == order_id), -1)
        if idx < 0:
            return jsonify({"ok": False, "error": "Order not found"}), 404

        current = entries[idx]
        previous_status = str(current.get("status") or "pending").strip().lower()
        products = read_products()
        inventory_reserved = bool(current.get("inventoryReserved", False))
        reservation = current.get("inventoryReservation") if isinstance(current.get("inventoryReservation"), list) else []

        if status == "canceled" and previous_status != "canceled" and inventory_reserved:
            restore_order_inventory(products, reservation)
            inventory_reserved = False
            write_products(products)
        elif status != "canceled" and previous_status == "canceled" and not inventory_reserved:
            reserved, inventory_error, reservation = reserve_order_inventory(products, current)
            if not reserved:
                return jsonify({"ok": False, "error": inventory_error, "code": "insufficient_stock"}), 409
            inventory_reserved = True
            write_products(products)

        merged = dict(current)
        merged["status"] = status
        merged["updatedAtMs"] = int(time.time() * 1000)
        merged["inventoryReserved"] = inventory_reserved
        merged["inventoryReservation"] = reservation
        item = normalize_order_item(merged, current)
        entries[idx] = item
        write_orders(entries)

    _notify_user_on_order_status_change(item, old_status=previous_status, new_status=status)

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

    original_name = str(file.filename or "").strip()
    safe = secure_filename(original_name)
    safe = re.sub(r"\s+", "_", safe)

    ext = Path(safe).suffix.lower().strip()
    if not ext:
        ext = Path(original_name).suffix.lower().strip()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify({"ok": False, "error": f"Unsupported image type: {ext or 'unknown'}"}), 400

    stem = Path(safe).stem.strip() if safe else ""
    if not stem:
        stem = f"img_{int(time.time() * 1000)}"
    safe = f"{stem}{ext}"

    mime = str(getattr(file, "mimetype", "") or "").strip().lower()
    # Some browsers/devices may send generic MIME types (e.g. application/octet-stream)
    # for valid image files. If extension is allowed, accept unless MIME is clearly non-image.
    if mime and not mime.startswith("image/") and mime not in {"application/octet-stream", "binary/octet-stream"}:
        return jsonify({"ok": False, "error": f"Invalid image MIME type: {mime}"}), 400

    max_bytes = _MAX_IMAGE_UPLOAD_MB * 1024 * 1024
    blob = file.stream.read(max_bytes + 1)
    if len(blob) > max_bytes:
        return jsonify({"ok": False, "error": f"Image exceeds max size of {_MAX_IMAGE_UPLOAD_MB}MB"}), 413
    if not blob:
        return jsonify({"ok": False, "error": "Uploaded image is empty"}), 400

    # Content-addressed names make retries idempotent: the same image does not
    # create duplicate files when a mobile connection drops after upload.
    digest = hashlib.sha256(blob).hexdigest()
    filename = f"{digest[:20]}_{safe}"
    dest = UPLOAD_DIR / filename
    created = not dest.exists()
    if created:
        dest.write_bytes(blob)

    url = f"{_request_public_base()}/uploads/{filename}"
    return jsonify({
        "ok": True,
        "created": created,
        "filename": filename,
        "url": url,
        "sizeBytes": dest.stat().st_size if dest.exists() else 0,
    })


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

    if item.get("productCode") and any(str(p.get("productCode", "")).strip().upper() == str(item.get("productCode", "")).strip().upper() for p in products):
        return jsonify({"ok": False, "error": "Product code already exists"}), 409

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

    if item.get("productCode") and any(i != idx and str(p.get("productCode", "")).strip().upper() == str(item.get("productCode", "")).strip().upper() for i, p in enumerate(products)):
        return jsonify({"ok": False, "error": "Product code already exists"}), 409

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
    image_url = str(payload.get("imageUrl") or "").strip()
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
                    "imageUrl": image_url,
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
            "imageUrl": image_url,
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
                "imageUrl": image_url,
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

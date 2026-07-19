# CarmenKarla Local Server (Python)

سيرفر محلي داخل الشبكة لإدارة المنتجات (إضافة/تعديل/حذف + رفع صور) باستخدام Python.

## 1) الإعداد

- انسخ `.env.example` إلى `.env`
- عدّل `API_TOKEN`
- ثبّت الحزم:
  - `pip install -r requirements.txt`

## 2) التشغيل

- `python server.py`

أو على ويندوز بضغطة واحدة:

- شغّل: `start_server.bat`

وللتحقق السريع من الصحة:

- `check_health.bat`
- أو بعنوان LAN محدد:
  - `check_health.bat http://192.168.1.50:8080`

لفتح المنفذ 8080 في الجدار الناري (اختياري، كمسؤول):

- `open_firewall_8080.ps1`

## 3) الوصول من الهاتف داخل نفس الشبكة

- استخدم IP الكمبيوتر، مثال:
  - `http://192.168.1.50:8080`

معلومة مهمة: إذا ما اشتغل من الهاتف بينما يعمل على نفس الكمبيوتر، غالبًا السبب جدار الحماية. وقتها شغّل:

- `open_firewall_8080.ps1` (PowerShell بصلاحية Administrator)

## 4) API

### عام
- `GET /health`
- `GET /products`
- `GET /products?includeHidden=1`

### إدارة (تحتاج Token)
ضع الهيدر:
- `Authorization: Bearer <API_TOKEN>`

المسارات:
- `POST /products`
- `PUT /products/<id>`
- `DELETE /products/<id>`
- `POST /products/upload` (form-data: `image`)
- `POST /notifications/send` (JSON)

#### إرسال إشعار للزبائن

Body (عام لكل المستخدمين):

- `title` (مطلوب)
- `body` (مطلوب)
- `audience` = `all`
- `limit` (اختياري، افتراضي 500، حد أقصى 2000)
- `target` (اختياري)
- `targetId` (اختياري)

Body (لمستخدم محدد):

- `title` (مطلوب)
- `body` (مطلوب)
- `audience` = `user`
- `userId` أو `userIds`
- `target` (اختياري)
- `targetId` (اختياري)

## 5) التخزين

- الوضع المحلي الافتراضي:
  - المنتجات: `local_server_py/data/products.json`
  - الصور: `local_server_py/uploads/`
- للوضع الاحترافي على Render مدفوع:
  - أضف Persistent Disk
  - اجعل `STORAGE_ROOT=/var/data/carmenkarla`
  - سيحفظ السيرفر البيانات في:
    - `/var/data/carmenkarla/data/products.json`
    - `/var/data/carmenkarla/uploads/`

### منع اختفاء المنتجات (إعداد احترافي موصى به)

يمكنك الآن اختيار محرك كتالوج المنتجات عبر متغيرات البيئة:

- `PRODUCTS_STORAGE_MODE=auto` (افتراضي)
  - يستخدم Firestore تلقائيًا إذا كانت بيانات Firebase متاحة، وإلا يستخدم ملف محلي.
- `PRODUCTS_STORAGE_MODE=local`
  - يجبر التخزين على `products.json` فقط.
- `PRODUCTS_STORAGE_MODE=firestore`
  - يجبر التخزين على Firestore (أفضل خيار للإنتاج).

ومتغير المجموعة:

- `PRODUCTS_FIRESTORE_COLLECTION=products_catalog`

> ملاحظة: حتى عند استخدام Firestore، السيرفر يحتفظ بنسخة محلية ونسخ احتياطية دورية داخل `data/backups/`.

### متغيرات البيئة المهمة على Render

- `API_TOKEN` = توكن إدارة لوحة الويب فقط
- `SERVER_BASE_URL` = `https://carmenkarla-backend.onrender.com`
- `STORAGE_ROOT` = `/var/data/carmenkarla`
- `CORS_ORIGIN` = اتركه فارغًا أو حدده حسب حاجتك
- `FIREBASE_SERVICE_ACCOUNT_FILE` = مسار ملف Service Account JSON (المفضل)
  - أو بديله: `FIREBASE_SERVICE_ACCOUNT_JSON` = محتوى JSON كسطر واحد
- `FIREBASE_PROJECT_ID` = اختياري
- `PRODUCTS_STORAGE_MODE` = `firestore` (موصى به للإنتاج)
- `PRODUCTS_FIRESTORE_COLLECTION` = `products_catalog`

### تفعيل التخزين الدائم (Persistent Disk) — خطوة إلزامية

1. في Render افتح الخدمة `carmenkarla-backend`.
2. من تبويب **Disks** أضف Disk بحجم 1GB (أو أكثر).
3. اجعل مسار الربط `mountPath` = `/var/data`.
4. تأكد أن متغير البيئة `STORAGE_ROOT` = `/var/data/carmenkarla`.
5. أعد النشر (Manual Deploy) بعد الحفظ.

> بعد التفعيل يجب أن يظهر في `/health`:
> - `storageMode: "persistent"`
> - `storageRoot` يبدأ بـ `/var/data`

### فحص الجاهزية من لوحة التحكم

في صفحة الحالة داخل لوحة الويب ستجد:

- `وضع التخزين`
- `محرك الكتالوج`
- `جاهزية الإنتاج`

إذا ظهرت `غير آمن بالكامل` فهذا يعني أنك ما زلت على تخزين مؤقت وقد يختفي المنتج بعد إعادة تشغيل الخدمة.

## 6) الربط مع التطبيق

من شاشة:
`إعدادات إشعارات الطلبات (للإدارة)`

- فعّل: `تفعيل Catalog محلي داخل الشبكة`
- أدخل Base URL (مثال: `http://192.168.1.50:8080`)
- اختبر الاتصال عبر زر `اختبار اتصال السيرفر المحلي`

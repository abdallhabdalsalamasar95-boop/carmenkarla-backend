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

## 5) التخزين

- المنتجات: `local_server_py/data/products.json`
- الصور: `local_server_py/uploads/`

## 6) الربط مع التطبيق

من شاشة:
`إعدادات إشعارات الطلبات (للإدارة)`

- فعّل: `تفعيل Catalog محلي داخل الشبكة`
- أدخل Base URL (مثال: `http://192.168.1.50:8080`)
- أدخل نفس `API_TOKEN`
- اختبر الاتصال عبر زر `اختبار اتصال السيرفر المحلي`

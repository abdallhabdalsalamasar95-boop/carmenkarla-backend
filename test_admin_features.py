import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class AdminFeatureTests(unittest.TestCase):
    def _inventory_api_state(self, size_quantities=None):
        products = [{
            "id": "dress-1",
            "name": "فستان",
            "sizes": "S,M,L",
            "sizeQuantities": size_quantities or {"S": 3, "M": 2, "L": 1},
            "stockQuantity": 6,
            "availableStock": 6,
            "lowStockThreshold": 1,
        }]
        orders = []

        def read_products():
            return products

        def write_products(value):
            products[:] = value

        def read_orders():
            return orders

        def write_orders(value):
            orders[:] = value

        return products, orders, read_products, write_products, read_orders, write_orders

    def _order_payload(self, order_id="order-stock", size="M", quantity=1):
        return {
            "orderId": order_id,
            "status": "pending",
            "payload": {
                "customer": {"name": "زبونة", "phone": "091"},
                "items": [{
                    "productId": "dress-1",
                    "name": "فستان",
                    "size": size,
                    "quantity": quantity,
                }],
                "pricing": {"grandTotal": 100},
            },
        }

    def test_website_home_normalizes_single_banner_and_ordered_categories(self):
        config = server.normalize_marketing_config({
            "websiteHome": {
                "banner": {
                    "imageUrl": " https://example.com/banner.jpg ",
                    "altText": "بانر الصيف",
                    "linkUrl": "#collection",
                    "enabled": True,
                },
                "categories": [
                    {
                        "id": "evening",
                        "title": " سهرة ",
                        "imageUrl": "https://example.com/evening.jpg",
                        "productCategoryFilter": "فساتين سهرة",
                        "sortOrder": 2,
                    },
                    {
                        "id": "new",
                        "title": "الجديد",
                        "imageUrl": "https://example.com/new.jpg",
                        "productCategoryFilter": "",
                        "sortOrder": 1,
                    },
                    {"id": "invalid", "title": ""},
                ],
            }
        })

        home = config["websiteHome"]
        self.assertEqual(
            home["banner"]["imageUrl"],
            "https://example.com/banner.jpg",
        )
        self.assertEqual(
            [item["id"] for item in home["categories"]],
            ["new", "evening"],
        )
        self.assertEqual(
            home["categories"][1]["productCategoryFilter"],
            "فساتين سهرة",
        )

    def test_size_quantities_define_total_stock(self):
        item = server.normalize_product({
            "name": "فستان",
            "sizes": "S,M,L",
            "sizeType": "clothing",
            "sizeQuantities": {"S": 2, "M": 4, "L": 1, "XL": 99},
            "stockQuantity": 500,
        })

        self.assertEqual(item["sizes"], "S,M,L")
        self.assertEqual(item["sizeQuantities"], {"S": 2, "M": 4, "L": 1})
        self.assertEqual(item["stockQuantity"], 7)
        self.assertEqual(item["availableStock"], 7)
        self.assertEqual(item["sizeType"], "clothing")

    def test_product_images_are_ordered_and_deduplicated(self):
        item = server.normalize_product({
            "name": "منتج بالصور",
            "imageUrl": "https://example.com/main.jpg",
            "imageUrls": [
                "https://example.com/second.jpg",
                "https://example.com/main.jpg",
                "https://example.com/second.jpg",
            ],
        })

        self.assertEqual(item["imageUrl"], "https://example.com/main.jpg")
        self.assertEqual(item["imageUrls"], [
            "https://example.com/main.jpg",
            "https://example.com/second.jpg",
        ])

    def test_ambassador_summary_survives_order_normalization(self):
        item = server.normalize_order_item({
            "orderId": "order-1",
            "payload": {
                "customer": {
                    "submitterUid": "amb-1",
                    "placedAsAmbassador": True,
                    "accountRole": "ambassador",
                },
                "ambassadorSummary": {
                    "isAmbassadorOrder": True,
                    "estimatedCommission": 12.5,
                },
            },
        })

        self.assertTrue(item["ambassadorSummary"]["isAmbassadorOrder"])
        self.assertEqual(item["ambassadorSummary"]["estimatedCommission"], 12.5)

    def test_ambassador_account_identity_is_preserved_separately_from_shipping(self):
        item = server.normalize_order_item({
            "orderId": "order-identity",
            "payload": {
                "customer": {
                    "name": "اسم مستلم الشحنة",
                    "phone": "091-shipping",
                    "submitterUid": "amb-22",
                    "submitterName": "المندوبة سارة",
                    "submitterEmail": "sara@example.com",
                    "submitterPhone": "092-ambassador",
                    "placedAsAmbassador": True,
                    "accountRole": "ambassador",
                },
            },
        })

        summary = item["ambassadorSummary"]
        self.assertEqual(summary["ambassadorUid"], "amb-22")
        self.assertEqual(summary["ambassadorName"], "المندوبة سارة")
        self.assertEqual(summary["ambassadorEmail"], "sara@example.com")
        self.assertEqual(summary["ambassadorPhone"], "092-ambassador")
        self.assertEqual(item["customerPhone"], "091-shipping")

    def test_order_decrements_selected_size_and_cancel_restores_it_once(self):
        products, orders, read_products, write_products, read_orders, write_orders = self._inventory_api_state()
        old_token = server.API_TOKEN
        server.API_TOKEN = "test-token"
        try:
            with patch.object(server, "read_products", side_effect=read_products), \
                 patch.object(server, "write_products", side_effect=write_products), \
                 patch.object(server, "read_orders", side_effect=read_orders), \
                 patch.object(server, "write_orders", side_effect=write_orders), \
                 patch.object(server, "_notify_user_on_order_status_change"):
                client = server.app.test_client()
                created = client.post("/orders", json=self._order_payload(quantity=2))
                self.assertEqual(created.status_code, 200)
                self.assertEqual(products[0]["sizeQuantities"], {"S": 3, "M": 0, "L": 1})
                self.assertEqual(products[0]["availableStock"], 4)
                self.assertTrue(orders[0]["inventoryReserved"])

                retry = client.post("/orders", json=self._order_payload(quantity=2))
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(products[0]["sizeQuantities"]["M"], 0)

                headers = {"Authorization": "Bearer test-token"}
                canceled = client.put("/orders/order-stock/status", json={"status": "canceled"}, headers=headers)
                self.assertEqual(canceled.status_code, 200)
                self.assertEqual(products[0]["sizeQuantities"], {"S": 3, "M": 2, "L": 1})
                self.assertFalse(orders[0]["inventoryReserved"])

                canceled_again = client.put("/orders/order-stock/status", json={"status": "canceled"}, headers=headers)
                self.assertEqual(canceled_again.status_code, 200)
                self.assertEqual(products[0]["sizeQuantities"]["M"], 2)

                reopened = client.put("/orders/order-stock/status", json={"status": "processing"}, headers=headers)
                self.assertEqual(reopened.status_code, 200)
                self.assertEqual(products[0]["sizeQuantities"]["M"], 0)
                self.assertTrue(orders[0]["inventoryReserved"])
        finally:
            server.API_TOKEN = old_token

    def test_order_rejects_insufficient_size_stock_without_partial_changes(self):
        products, orders, read_products, write_products, read_orders, write_orders = self._inventory_api_state()
        with patch.object(server, "read_products", side_effect=read_products), \
             patch.object(server, "write_products", side_effect=write_products), \
             patch.object(server, "read_orders", side_effect=read_orders), \
             patch.object(server, "write_orders", side_effect=write_orders):
            response = server.app.test_client().post("/orders", json=self._order_payload(quantity=3))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "insufficient_stock")
        self.assertEqual(products[0]["sizeQuantities"]["M"], 2)
        self.assertEqual(orders, [])

    def test_order_matches_clean_size_to_legacy_bracketed_inventory_key(self):
        products, orders, read_products, write_products, read_orders, write_orders = self._inventory_api_state(
            {"['S'": 2, "'M'": 3, "'L']": 1},
        )
        with patch.object(server, "read_products", side_effect=read_products), \
             patch.object(server, "write_products", side_effect=write_products), \
             patch.object(server, "read_orders", side_effect=read_orders), \
             patch.object(server, "write_orders", side_effect=write_orders):
            response = server.app.test_client().post("/orders", json=self._order_payload(size="M", quantity=2))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(products[0]["sizeQuantities"]["'M'"], 1)
        self.assertEqual(orders[0]["inventoryReservation"][0]["storedSizeKey"], "'M'")

    def test_admin_page_is_mobile_and_not_cached(self):
        response = server.app.test_client().get("/admin")
        try:
            body = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers.get("Cache-Control"),
                "no-store, no-cache, must-revalidate, max-age=0",
            )
            self.assertIn("user-scalable=no", body)
            self.assertIn("flex: 1 1 auto; min-height: 0; overflow-y: auto", body)
            self.assertIn("sizeQuantityEditor", body)
            self.assertIn("selectAllVisibleSizes", body)
            self.assertIn("quantity-stepper", body)
            self.assertIn("changeSizeQuantity", body)
            self.assertIn("inputmode=\"none\" readonly", body)
            self.assertIn("uploadOneImage", body)
            self.assertIn("class=\"native-file-input\"", body)
            self.assertIn("for=\"fileInput\"", body)
            self.assertIn("multiple onchange=\"handleFileSelect(event)\"", body)
            self.assertIn("ambassadorDetailModal", body)
            self.assertIn("ambassadorSearch", body)
        finally:
            response.close()

    def test_multiple_images_can_be_uploaded_sequentially(self):
        old_token = server.API_TOKEN
        old_upload_dir = server.UPLOAD_DIR
        try:
            server.API_TOKEN = "test-token"
            with tempfile.TemporaryDirectory() as temp_dir:
                server.UPLOAD_DIR = Path(temp_dir)
                client = server.app.test_client()
                headers = {"Authorization": "Bearer test-token"}

                first = client.post(
                    "/products/upload",
                    data={"image": (io.BytesIO(b"first-image"), "first.jpg")},
                    headers=headers,
                    content_type="multipart/form-data",
                )
                second = client.post(
                    "/products/upload",
                    data={"image": (io.BytesIO(b"second-image"), "second.png")},
                    headers=headers,
                    content_type="multipart/form-data",
                )

                self.assertEqual(first.status_code, 200)
                self.assertEqual(second.status_code, 200)
                self.assertNotEqual(first.get_json()["url"], second.get_json()["url"])
                self.assertEqual(len(list(Path(temp_dir).iterdir())), 2)
        finally:
            server.API_TOKEN = old_token
            server.UPLOAD_DIR = old_upload_dir

    def test_retrying_the_same_image_is_idempotent(self):
        old_token = server.API_TOKEN
        old_upload_dir = server.UPLOAD_DIR
        try:
            server.API_TOKEN = "test-token"
            with tempfile.TemporaryDirectory() as temp_dir:
                server.UPLOAD_DIR = Path(temp_dir)
                client = server.app.test_client()
                headers = {"Authorization": "Bearer test-token"}
                payload = b"same-image-content"

                first = client.post(
                    "/products/upload",
                    data={"image": (io.BytesIO(payload), "photo.jpg")},
                    headers=headers,
                    content_type="multipart/form-data",
                )
                retry = client.post(
                    "/products/upload",
                    data={"image": (io.BytesIO(payload), "photo.jpg")},
                    headers=headers,
                    content_type="multipart/form-data",
                )

                self.assertEqual(first.status_code, 200)
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(first.get_json()["url"], retry.get_json()["url"])
                self.assertTrue(first.get_json()["created"])
                self.assertFalse(retry.get_json()["created"])
                self.assertEqual(len(list(Path(temp_dir).iterdir())), 1)
        finally:
            server.API_TOKEN = old_token
            server.UPLOAD_DIR = old_upload_dir

    def test_oversized_image_is_rejected_without_writing_a_file(self):
        old_token = server.API_TOKEN
        old_upload_dir = server.UPLOAD_DIR
        old_limit = server._MAX_IMAGE_UPLOAD_MB
        try:
            server.API_TOKEN = "test-token"
            server._MAX_IMAGE_UPLOAD_MB = 1
            with tempfile.TemporaryDirectory() as temp_dir:
                server.UPLOAD_DIR = Path(temp_dir)
                response = server.app.test_client().post(
                    "/products/upload",
                    data={"image": (io.BytesIO(b"x" * (1024 * 1024 + 1)), "large.jpg")},
                    headers={"Authorization": "Bearer test-token"},
                    content_type="multipart/form-data",
                )

                self.assertEqual(response.status_code, 413)
                self.assertEqual(len(list(Path(temp_dir).iterdir())), 0)
        finally:
            server.API_TOKEN = old_token
            server.UPLOAD_DIR = old_upload_dir
            server._MAX_IMAGE_UPLOAD_MB = old_limit


if __name__ == "__main__":
    unittest.main()

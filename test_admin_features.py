import io
import tempfile
import unittest
from pathlib import Path

import server


class AdminFeatureTests(unittest.TestCase):
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
            self.assertIn("uploadOneImage", body)
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

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
            self.assertIn("sizeQuantityEditor", body)
            self.assertIn("ambassadorDetailModal", body)
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


if __name__ == "__main__":
    unittest.main()

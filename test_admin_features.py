import base64
import io
import json
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

    def test_product_ambassador_commission_is_persisted_and_clamped(self):
        item = server.normalize_product({
            "name": "فستان بعمولة",
            "commissionPercent": 12.5,
        })
        self.assertEqual(item["commissionPercent"], 12.5)

        self.assertEqual(
            server.normalize_product({"commissionPercent": -4})["commissionPercent"],
            0,
        )
        self.assertEqual(
            server.normalize_product({"commissionPercent": 140})["commissionPercent"],
            100,
        )

    def test_order_purchase_price_snapshot_is_authoritative_and_stable(self):
        order = server.normalize_order_item({
            "orderId": "cost-order",
            "payload": {"items": [{"productId": "p1", "quantity": 2, "purchasePrice": 1}]},
        })
        snapped = server.snapshot_order_purchase_costs(order, [{"id": "p1", "purchasePrice": 40}])
        self.assertEqual(snapped["payload"]["items"][0]["purchasePrice"], 40)

        retried = server.snapshot_order_purchase_costs(
            order,
            [{"id": "p1", "purchasePrice": 55}],
            snapped,
        )
        self.assertEqual(retried["payload"]["items"][0]["purchasePrice"], 40)

    def test_sabil_payload_maps_customer_and_all_order_items(self):
        order = server.normalize_order_item({
            "orderId": "order-sabil-1",
            "payload": {
                "customer": {
                    "name": "سارة",
                    "phone": "0912345678",
                    "city": "طرابلس",
                    "area": "حي الأندلس",
                    "address": "حي الأندلس",
                },
                "items": [{
                    "productId": "p1",
                    "name": "فستان",
                    "price": 120,
                    "quantity": 2,
                    "size": "M",
                    "color": "أسود",
                }],
                "note": "الاتصال قبل الوصول",
            },
        })
        with patch.object(server, "_SABIL_SERVICE_ID", "service-1"), \
             patch.object(server, "_SABIL_CONTACT_IDS", ["contact-1"]):
            payload = server.build_sabil_shipment_payload(order)

        self.assertEqual(payload["service"], "service-1")
        self.assertEqual(payload["contacts"], ["contact-1"])
        self.assertEqual(payload["to"]["city"], "طرابلس")
        self.assertEqual(payload["to"]["area"], "حي الأندلس")
        self.assertEqual(payload["to"]["address"], "حي الأندلس")
        self.assertEqual(payload["products"][0]["quantity"], 2)
        self.assertEqual(payload["products"][0]["currency"], "lyd")
        self.assertEqual(payload["products"][0]["widthCM"], 10)
        self.assertTrue(payload["products"][0]["allowInspection"])
        self.assertTrue(payload["products"][0]["allowTesting"])
        self.assertFalse(payload["products"][0]["isFragile"])
        self.assertNotIn("metadata", payload["products"][0])
        self.assertEqual(payload["allowedBankNotes"], {"50": False})
        self.assertEqual(payload["tags"], [])
        self.assertEqual(payload["metadata"], {})

    def test_sabil_payload_maps_yefren_to_provider_geo_hierarchy(self):
        order = server.normalize_order_item({
            "orderId": "order-yefren",
            "payload": {
                "customer": {
                    "name": "عبدالله عصر",
                    "phone": "0921307674",
                    "city": "يفرن",
                    "area": "الغنائمة",
                    "address": "الغنائمة",
                },
                "items": [{"name": "منتج اختبار", "price": 155, "quantity": 1}],
            },
        })
        with patch.object(server, "_SABIL_COUNTRY_CODE", "lby"):
            payload = server.build_sabil_shipment_payload(order, ["contact-1"])

        self.assertEqual(payload["to"], {
            "countryCode": "lby",
            "city": "غريان",
            "area": "يفرن",
            "address": "الغنائمة",
        })

    def test_sabil_contact_reuses_existing_customer_phone(self):
        order = server.normalize_order_item({
            "orderId": "contact-existing",
            "payload": {"customer": {"name": "سارة", "phone": "+218 91 234 5678"}},
        })
        response = {
            "status": True,
            "data": {"results": [{"_id": "contact-9", "phone": "0912345678"}]},
        }
        with patch.object(server, "_SABIL_CONTACT_IDS", []), \
             patch.object(server, "_request_sabil_api", return_value=(200, response)) as request_api:
            contact_id = server._sabil_contact_for_order(order)

        self.assertEqual(contact_id, "contact-9")
        request_api.assert_called_once_with("/api/contacts/")

    def test_sabil_contact_matches_local_and_international_libyan_phone(self):
        response = {"data": [{"_id": "contact-218", "phone": "+218 91 234 5678"}]}

        self.assertEqual(
            server._matching_sabil_contact_id(response, "091-234-5678"),
            "contact-218",
        )
        self.assertEqual(server._sabil_contact_phone("091-234-5678"), "+218912345678")
        self.assertEqual(server._sabil_contact_phone("00218 91 234 5678"), "+218912345678")

    def test_sabil_headers_follow_official_api_contract(self):
        with patch.object(server, "_SABIL_API_KEY", "secret"), \
             patch.object(server, "_SABIL_ACCESS_TOKEN", ""), \
             patch.object(server, "_SABIL_ACCOUNT_ID", "account"), \
             patch.object(server, "_SABIL_API_VERSION", "1.0.0"):
            headers = server._sabil_headers()

        self.assertEqual(headers["Authorization"], "apikey secret")
        self.assertEqual(headers["X-ACCOUNT-ID"], "account")
        self.assertEqual(headers["Origin"], "https://app.sabil.ly")
        self.assertIn("Chrome/148", headers["User-Agent"])
        self.assertEqual(headers["Sec-CH-UA-Platform"], '"Windows"')
        self.assertNotIn("X-API-Key", headers)

    def test_admin_can_preview_sabil_shipping_without_creating_shipment(self):
        old_token = server.API_TOKEN
        server.API_TOKEN = "test-token"
        try:
            with patch.object(
                server,
                "preview_sabil_shipping",
                return_value={"status": "ready", "httpStatus": 200, "response": {}},
            ) as preview:
                response = server.app.test_client().post(
                    "/orders/order-preview/delivery/darb-sabeel/preview",
                    headers={"Authorization": "Bearer test-token"},
                )
        finally:
            server.API_TOKEN = old_token

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["preview"]["httpStatus"], 200)
        preview.assert_called_once_with("order-preview")

    def test_sabil_portal_session_is_preferred_and_counts_as_configured(self):
        with patch.object(server, "_SABIL_ENABLED", True), \
             patch.object(server, "_SABIL_API_KEY", "restricted-api-key"), \
             patch.object(server, "_SABIL_ACCESS_TOKEN", "session-access-token"), \
             patch.object(server, "_SABIL_REFRESH_TOKEN", "session-refresh-token"), \
             patch.object(server, "_SABIL_ACCOUNT_ID", "account"), \
             patch.object(server, "_SABIL_SERVICE_ID", "service"):
            config = server.sabil_config_status()
            headers = server._sabil_headers()

        self.assertTrue(config["ready"])
        self.assertEqual(config["authMode"], "session")
        self.assertTrue(config["sessionRefreshConfigured"])
        self.assertEqual(headers["Authorization"], "Bearer session-access-token")

    def test_sabil_newest_complete_session_wins_across_restart(self):
        def token(issued_at):
            payload = base64.urlsafe_b64encode(
                ('{"iat":%s,"exp":9999999999}' % issued_at).encode(),
            ).decode().rstrip("=")
            return f"header.{payload}.signature"

        with tempfile.TemporaryDirectory() as temp_dir:
            session_file = Path(temp_dir) / "sabil_session.json"
            session_file.write_text(
                json.dumps({
                    "accessToken": token(200),
                    "refreshToken": "newer-persisted-refresh",
                }),
                encoding="utf-8",
            )
            with patch.object(server, "_SABIL_SESSION_FILE", session_file), \
                 patch.object(server, "_SABIL_ACCESS_TOKEN", token(100)), \
                 patch.object(server, "_SABIL_REFRESH_TOKEN", "older-environment-refresh"):
                server._load_sabil_session()
                self.assertEqual(server._SABIL_ACCESS_TOKEN, token(200))
                self.assertEqual(server._SABIL_REFRESH_TOKEN, "newer-persisted-refresh")

    def test_sabil_shipment_captures_provider_underscore_id(self):
        order = server.normalize_order_item(self._order_payload(order_id="sabil-provider-shape"))
        with patch.object(server, "sabil_config_status", return_value={"ready": True, "missing": []}), \
             patch.object(server, "_sabil_contact_for_order", return_value="contact-1"), \
             patch.object(
                 server,
                 "_request_sabil_api",
                 return_value=(201, {"data": {"_id": "shipment-1", "reference": "SH123"}}),
             ):
            result = server._request_sabil_shipment(order)

        self.assertEqual(result["shipmentId"], "shipment-1")
        self.assertEqual(result["trackingNumber"], "shipment-1")
        self.assertEqual(result["referenceCode"], "SH123")

    def test_sabil_invalid_cross_city_area_falls_back_to_city_center(self):
        payload = {
            "to": {
                "countryCode": "lby",
                "city": "مصراتة",
                "area": "عين زارة",
                "address": "العنوان التفصيلي",
            },
            "products": [],
        }
        with patch.object(
            server,
            "_request_sabil_api",
            side_effect=[
                RuntimeError("Unable to fetch branch 'LBY-مصراتة,عين زارة'!"),
                (201, {"data": {"_id": "shipment-1"}}),
            ],
        ) as request_api:
            status, _ = server._request_sabil_with_branch_fallback(
                "/api/local/shipments",
                payload,
            )

        self.assertEqual(status, 201)
        retry_payload = request_api.call_args_list[1].kwargs["payload"]
        self.assertEqual(retry_payload["to"]["city"], "مصراتة")
        self.assertEqual(retry_payload["to"]["area"], "مصراتة")
        self.assertEqual(retry_payload["to"]["address"], "العنوان التفصيلي")
        self.assertEqual(payload["to"]["area"], "عين زارة")

    def test_sabil_destinations_group_all_areas_by_city(self):
        provider_response = {
            "data": {
                "results": [
                    {"countryCode": "lby", "city": "طرابلس", "area": "عين زارة", "areas": [{"area": "تاجوراء"}, {"area": "سوق الجمعة"}]},
                    {"countryCode": "lby", "city": "طرابلس", "area": "سوق الجمعة"},
                    {"countryCode": "lby", "city": "مصراتة", "area": "مصراتة"},
                    {"countryCode": "tun", "city": "تونس", "area": "المرسى"},
                ],
            },
        }
        with patch.object(server, "_SABIL_DESTINATIONS_CACHE", {"expiresAt": 0, "cities": {}}), \
             patch.object(server, "_request_sabil_api", return_value=(200, provider_response)):
            cities = server.sabil_delivery_destinations()

        self.assertEqual(cities["طرابلس"], ["تاجوراء", "سوق الجمعة", "عين زارة"])
        self.assertEqual(cities["مصراتة"], ["مصراتة"])
        self.assertNotIn("تونس", cities)

    def test_public_sabil_destinations_endpoint(self):
        with patch.object(server, "sabil_delivery_destinations", return_value={"بنغازي": ["البركة"]}):
            response = server.app.test_client().get("/delivery/darb-sabeel/destinations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["cities"], {"بنغازي": ["البركة"]})

    def test_sabil_shipment_snapshot_distinguishes_existing_and_deleted(self):
        existing_response = {
            "status": 200,
            "data": {"results": [{"_id": "shipment-1", "reference": "SH1", "status": "pending"}]},
        }
        with patch.object(server, "_request_sabil_api", return_value=(200, existing_response)):
            existing = server._sabil_shipment_snapshot("shipment-1")
        with patch.object(server, "_request_sabil_api", return_value=(200, {"data": {"results": []}})):
            deleted = server._sabil_shipment_snapshot("shipment-1")
        with patch.object(server, "_request_sabil_api", side_effect=RuntimeError("Darb Al Sabeel HTTP 404: Not Found")):
            missing = server._sabil_shipment_snapshot("shipment-1")

        self.assertEqual(existing, {"exists": True, "deleted": False, "providerStatus": "pending"})
        self.assertTrue(deleted["deleted"])
        self.assertTrue(missing["deleted"])

    def test_sabil_deleted_shipment_cancels_order_and_restores_stock_once(self):
        products, orders, read_products, write_products, read_orders, write_orders = self._inventory_api_state()
        with patch.object(server, "_SABIL_ENABLED", False), \
             patch.object(server, "read_products", side_effect=read_products), \
             patch.object(server, "write_products", side_effect=write_products), \
             patch.object(server, "read_orders", side_effect=read_orders), \
             patch.object(server, "write_orders", side_effect=write_orders), \
             patch.object(server, "_notify_user_on_order_status_change") as notify:
            created = server.app.test_client().post("/orders", json=self._order_payload(order_id="provider-delete", quantity=2))
            self.assertEqual(created.status_code, 200)
            orders[0]["externalDelivery"] = {
                "provider": "darb_sabeel",
                "status": "created",
                "shipmentId": "shipment-delete-1",
            }
            with patch.object(server, "_sabil_shipment_snapshot", return_value={
                "exists": False,
                "deleted": True,
                "providerStatus": "deleted",
            }):
                first = server.sync_sabil_deleted_shipments()
                second = server.sync_sabil_deleted_shipments()

        self.assertEqual(first["canceled"], 1)
        self.assertEqual(second["canceled"], 0)
        self.assertEqual(orders[0]["status"], "canceled")
        self.assertFalse(orders[0]["inventoryReserved"])
        self.assertEqual(products[0]["sizeQuantities"]["M"], 2)
        self.assertEqual(orders[0]["externalDelivery"]["syncStatus"], "deleted_on_provider")
        notify.assert_called_once()

    def test_new_customer_and_ambassador_orders_auto_dispatch_once(self):
        products, orders, read_products, write_products, read_orders, write_orders = self._inventory_api_state(
            {"S": 3, "M": 3, "L": 1},
        )
        customer_order = self._order_payload(order_id="customer-auto-sabil", quantity=1)
        ambassador_order = self._order_payload(order_id="ambassador-auto-sabil", quantity=1)
        with patch.object(server, "_SABIL_ENABLED", True), \
             patch.object(server, "read_products", side_effect=read_products), \
             patch.object(server, "write_products", side_effect=write_products), \
             patch.object(server, "read_orders", side_effect=read_orders), \
             patch.object(server, "write_orders", side_effect=write_orders), \
             patch.object(server, "dispatch_order_to_sabil", return_value={"status": "created"}) as dispatch, \
             patch.object(server, "_firebase_user_from_request", return_value=({"uid": "amb-1"}, None)), \
             patch.object(server, "_firebase_user_profile", return_value={"accountRole": "ambassador", "ambassadorName": "سارة"}):
            client = server.app.test_client()
            customer = client.post("/orders", json=customer_order)
            customer_retry = client.post("/orders", json=customer_order)
            ambassador = client.post(
                "/orders",
                json=ambassador_order,
                headers={"Authorization": "Bearer firebase-token"},
            )
            ambassador_retry = client.post(
                "/orders",
                json=ambassador_order,
                headers={"Authorization": "Bearer firebase-token"},
            )

        self.assertEqual(customer.status_code, 200)
        self.assertTrue(customer.get_json()["created"])
        self.assertFalse(customer_retry.get_json()["created"])
        self.assertEqual(ambassador.status_code, 200)
        self.assertTrue(ambassador.get_json()["created"])
        self.assertFalse(ambassador_retry.get_json()["created"])
        self.assertEqual(
            [call.args[0] for call in dispatch.call_args_list],
            ["customer-auto-sabil", "ambassador-auto-sabil"],
        )
        self.assertEqual(len(orders), 2)
        self.assertEqual(products[0]["sizeQuantities"]["M"], 1)

    def test_sabil_contact_is_created_for_new_customer(self):
        order = server.normalize_order_item({
            "orderId": "contact-new",
            "payload": {"customer": {"name": "سارة", "phone": "0912345678"}},
        })
        with patch.object(server, "_SABIL_CONTACT_IDS", []), \
             patch.object(server, "_request_sabil_api", side_effect=[
                 (200, {"status": True, "data": {"results": []}}),
                 (201, {"status": True, "data": {"_id": "contact-new-1"}}),
             ]) as request_api:
            contact_id = server._sabil_contact_for_order(order)

        self.assertEqual(contact_id, "contact-new-1")
        self.assertEqual(request_api.call_count, 2)
        request_api.assert_called_with(
            "/api/contacts",
            method="POST",
            payload={"name": "سارة", "phone": "+218912345678"},
        )

    def test_sabil_configuration_does_not_require_fixed_contact_ids(self):
        with patch.object(server, "_SABIL_ENABLED", True), \
             patch.object(server, "_SABIL_API_KEY", "key"), \
             patch.object(server, "_SABIL_ACCOUNT_ID", "account"), \
             patch.object(server, "_SABIL_SERVICE_ID", "service"), \
             patch.object(server, "_SABIL_CONTACT_IDS", []):
            config = server.sabil_config_status()

        self.assertTrue(config["ready"])
        self.assertNotIn("SABIL_CONTACT_IDS", config["missing"])

    def test_sabil_dispatch_is_idempotent_after_shipment_creation(self):
        orders = [server.normalize_order_item(self._order_payload(order_id="sabil-once"))]

        def write_orders(items):
            orders[:] = items

        provider_result = {
            "provider": "darb_sabeel",
            "status": "created",
            "shipmentId": "shipment-7",
            "trackingNumber": "TRACK-7",
            "referenceCode": "",
            "httpStatus": 201,
            "lastError": "",
        }
        with patch.object(server, "read_orders", side_effect=lambda: orders), \
             patch.object(server, "write_orders", side_effect=write_orders), \
             patch.object(server, "_request_sabil_shipment", return_value=provider_result) as sender:
            first = server.dispatch_order_to_sabil("sabil-once")
            second = server.dispatch_order_to_sabil("sabil-once")

        self.assertEqual(first["trackingNumber"], "TRACK-7")
        self.assertEqual(second["trackingNumber"], "TRACK-7")
        sender.assert_called_once()

    def test_admin_can_idempotently_attach_provider_created_sabil_shipment(self):
        orders = [server.normalize_order_item(self._order_payload(order_id="sabil-portal"))]

        def write_orders(items):
            orders[:] = items

        old_token = server.API_TOKEN
        server.API_TOKEN = "test-token"
        try:
            with patch.object(server, "read_orders", side_effect=lambda: orders), \
                 patch.object(server, "write_orders", side_effect=write_orders):
                client = server.app.test_client()
                headers = {"Authorization": "Bearer test-token"}
                payload = {
                    "shipmentId": "shipment-portal-1",
                    "trackingNumber": "TRACK-PORTAL-1",
                    "referenceCode": "REF-1",
                    "httpStatus": 201,
                }
                first = client.post(
                    "/orders/sabil-portal/delivery/darb-sabeel/attach",
                    json=payload,
                    headers=headers,
                )
                retry = client.post(
                    "/orders/sabil-portal/delivery/darb-sabeel/attach",
                    json=payload,
                    headers=headers,
                )
        finally:
            server.API_TOKEN = old_token

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(orders[0]["externalDelivery"]["status"], "created")
        self.assertEqual(orders[0]["externalDelivery"]["shipmentId"], "shipment-portal-1")

    def test_attach_sabil_shipment_rejects_different_existing_shipment(self):
        order = server.normalize_order_item(self._order_payload(order_id="sabil-conflict"))
        order["externalDelivery"] = {
            "provider": "darb_sabeel",
            "status": "created",
            "shipmentId": "shipment-existing",
        }
        with patch.object(server, "read_orders", return_value=[order]):
            with self.assertRaisesRegex(RuntimeError, "different shipment"):
                server.attach_sabil_shipment(
                    "sabil-conflict",
                    {"shipmentId": "shipment-other"},
                )

    def test_admin_sabil_status_is_protected_and_does_not_expose_secrets(self):
        old_token = server.API_TOKEN
        server.API_TOKEN = "test-token"
        try:
            client = server.app.test_client()
            unauthorized = client.get("/admin/delivery/darb-sabeel/status")
            authorized = client.get(
                "/admin/delivery/darb-sabeel/status",
                headers={"Authorization": "Bearer test-token"},
            )
        finally:
            server.API_TOKEN = old_token

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        body = authorized.get_json()
        self.assertNotIn("apiKey", body["config"])
        self.assertNotIn("accountId", body["config"])

    def test_accounting_summary_calculates_net_profit_and_inventory_value(self):
        products = [{
            "id": "p1",
            "name": "فستان",
            "price": 100,
            "purchasePrice": 40,
            "stockQuantity": 3,
            "availableStock": 3,
        }]
        orders = [server.normalize_order_item({
            "orderId": "delivered-accounting",
            "status": "delivered",
            "grandTotal": 200,
            "ambassadorSummary": {
                "isAmbassadorOrder": True,
                "estimatedCommission": 20,
            },
            "payload": {
                "pricing": {"grandTotal": 200},
                "items": [{
                    "productId": "p1",
                    "name": "فستان",
                    "price": 100,
                    "purchasePrice": 40,
                    "quantity": 2,
                }],
            },
        })]
        expenses = [{"id": "e1", "amount": 30, "description": "توصيل", "expenseAtMs": 1}]
        with patch.object(server, "read_products", return_value=products), \
             patch.object(server, "read_orders", return_value=orders), \
             patch.object(server, "read_expenses", return_value=expenses):
            summary = server.accounting_summary()

        self.assertEqual(summary["revenue"], 200)
        self.assertEqual(summary["costOfGoods"], 80)
        self.assertEqual(summary["grossProfit"], 120)
        self.assertEqual(summary["ambassadorCommissions"], 20)
        self.assertEqual(summary["expenses"], 30)
        self.assertEqual(summary["netProfit"], 70)
        self.assertEqual(summary["inventoryPieces"], 3)
        self.assertEqual(summary["inventoryCostValue"], 120)
        self.assertEqual(summary["inventorySaleValue"], 300)
        self.assertEqual(summary["inventoryPotentialProfit"], 180)

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

    def test_size_with_one_piece_rejects_quantity_two(self):
        products, orders, read_products, write_products, read_orders, write_orders = self._inventory_api_state(
            {"S": 2, "M": 1, "L": 1},
        )
        with patch.object(server, "read_products", side_effect=read_products), \
             patch.object(server, "write_products", side_effect=write_products), \
             patch.object(server, "read_orders", side_effect=read_orders), \
             patch.object(server, "write_orders", side_effect=write_orders):
            response = server.app.test_client().post("/orders", json=self._order_payload(size="M", quantity=2))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "insufficient_stock")
        self.assertEqual(products[0]["sizeQuantities"]["M"], 1)
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

    def test_secure_ambassador_feed_returns_only_authenticated_users_orders(self):
        orders = [
            server.normalize_order_item({
                "orderId": "mine",
                "status": "delivered",
                "payload": {
                    "customer": {"name": "عميلة 1", "phone": "091", "address": "حي الأندلس", "city": "طرابلس", "submitterUid": "amb-1", "accountRole": "ambassador"},
                    "items": [{"productId": "p1", "name": "فستان سهرة", "imageUrl": "https://example.com/dress.jpg", "size": "M", "color": "أسود", "quantity": 2, "price": 100}],
                    "pricing": {"grandTotal": 100},
                },
            }),
            server.normalize_order_item({
                "orderId": "other",
                "payload": {
                    "customer": {"name": "عميلة 2", "submitterUid": "amb-2", "accountRole": "ambassador"},
                    "items": [{"productId": "p2", "quantity": 1, "price": 200}],
                    "pricing": {"grandTotal": 200},
                },
            }),
        ]
        with patch.object(server, "_firebase_user_from_request", return_value=({"uid": "amb-1"}, None)), \
             patch.object(server, "_firebase_user_profile", return_value={"accountRole": "ambassador"}), \
             patch.object(server, "read_orders", return_value=orders):
            response = server.app.test_client().get("/ambassadors/me/orders")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["orderId"], "mine")
        self.assertEqual(payload["items"][0]["customerName"], "عميلة 1")
        self.assertEqual(payload["items"][0]["customerAddress"], "حي الأندلس")
        self.assertEqual(payload["items"][0]["customerCity"], "طرابلس")
        self.assertEqual(payload["items"][0]["itemsCount"], 2)
        self.assertEqual(payload["items"][0]["payload"]["items"][0]["size"], "M")

    def test_secure_ambassador_feed_rejects_regular_customer(self):
        with patch.object(server, "_firebase_user_from_request", return_value=({"uid": "customer-1"}, None)), \
             patch.object(server, "_firebase_user_profile", return_value={"accountRole": "customer"}):
            response = server.app.test_client().get("/ambassadors/me/orders")

        self.assertEqual(response.status_code, 403)

    def _delivered_ambassador_order(self, amount=90.0):
        return server.normalize_order_item({
            "orderId": f"delivered-{amount}",
            "status": "delivered",
            "ambassadorSummary": {
                "isAmbassadorOrder": True,
                "ambassadorUid": "amb-1",
                "estimatedCommission": amount,
            },
            "payload": {
                "customer": {"submitterUid": "amb-1", "accountRole": "ambassador"},
                "items": [{"productId": "p1", "price": amount, "quantity": 1, "commissionPercent": 100}],
                "pricing": {"grandTotal": amount},
            },
        })

    def test_ambassador_withdrawal_is_blocked_below_100_lyd(self):
        with patch.object(server, "_firebase_user_from_request", return_value=({"uid": "amb-1"}, None)), \
             patch.object(server, "_firebase_user_profile", return_value={"accountRole": "ambassador"}), \
             patch.object(server, "read_orders", return_value=[self._delivered_ambassador_order(99)]), \
             patch.object(server, "read_ambassador_withdrawals", return_value=[]):
            response = server.app.test_client().post("/ambassadors/me/withdrawals")

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["code"], "minimum_not_reached")
        self.assertEqual(payload["available"], 99)
        self.assertEqual(payload["remainingToMinimum"], 1)

    def test_ambassador_can_withdraw_full_available_balance_at_100_lyd(self):
        withdrawals = []

        def write_withdrawals(items):
            withdrawals[:] = items

        with patch.object(server, "_firebase_user_from_request", return_value=({"uid": "amb-1"}, None)), \
             patch.object(server, "_firebase_user_profile", return_value={"accountRole": "ambassador", "ambassadorName": "سارة"}), \
             patch.object(server, "read_orders", return_value=[self._delivered_ambassador_order(125)]), \
             patch.object(server, "read_ambassador_withdrawals", side_effect=lambda: withdrawals), \
             patch.object(server, "write_ambassador_withdrawals", side_effect=write_withdrawals):
            response = server.app.test_client().post("/ambassadors/me/withdrawals")

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["request"]["amount"], 125)
        self.assertEqual(payload["request"]["status"], "pending")
        self.assertEqual(payload["available"], 0)
        self.assertFalse(payload["canRequest"])

    def test_ambassador_cannot_create_duplicate_pending_withdrawal(self):
        pending = [{
            "id": "wd-existing",
            "ambassadorUid": "amb-1",
            "amount": 100,
            "status": "pending",
            "createdAtMs": 1,
            "updatedAtMs": 1,
        }]
        with patch.object(server, "_firebase_user_from_request", return_value=({"uid": "amb-1"}, None)), \
             patch.object(server, "_firebase_user_profile", return_value={"accountRole": "ambassador"}), \
             patch.object(server, "read_orders", return_value=[self._delivered_ambassador_order(250)]), \
             patch.object(server, "read_ambassador_withdrawals", return_value=pending):
            response = server.app.test_client().post("/ambassadors/me/withdrawals")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "withdrawal_pending")

    def test_ambassador_profile_can_be_saved_by_authenticated_owner(self):
        with patch.object(server, "_firebase_user_from_request", return_value=({"uid": "amb-7", "email": "a@example.com"}, None)), \
             patch.object(server, "_firebase_user_profile", return_value={}), \
             patch.object(server, "_save_firebase_user_profile", return_value=(True, "")) as save_profile:
            response = server.app.test_client().put("/ambassadors/me/profile", json={
                "ambassadorName": "سارة محمد",
                "ambassadorPhone": "091-234-5678",
                "ambassadorAddress": "طرابلس - الأندلس",
            })

        self.assertEqual(response.status_code, 200)
        profile = response.get_json()["profile"]
        self.assertEqual(profile["uid"], "amb-7")
        self.assertEqual(profile["ambassadorPhone"], "0912345678")
        self.assertEqual(profile["accountRole"], "ambassador")
        save_profile.assert_called_once()

    def test_ambassador_profile_rejects_invalid_phone(self):
        with patch.object(server, "_firebase_user_from_request", return_value=({"uid": "amb-7"}, None)):
            response = server.app.test_client().put("/ambassadors/me/profile", json={
                "ambassadorName": "سارة محمد",
                "ambassadorPhone": "123",
                "ambassadorAddress": "طرابلس",
            })

        self.assertEqual(response.status_code, 400)

    def test_admin_ambassadors_returns_registered_profiles(self):
        old_token = server.API_TOKEN
        server.API_TOKEN = "test-token"
        try:
            with patch.object(server, "_firebase_ambassador_profiles", return_value=([{
                "uid": "amb-registered",
                "accountRole": "ambassador",
                "ambassadorName": "مريم محمد",
                "ambassadorPhone": "0912345678",
                "ambassadorAddress": "طرابلس",
                "email": "maryam@example.com",
                "status": "active",
                "joinedAt": 123,
                "updatedAt": 456,
            }], "")):
                response = server.app.test_client().get(
                    "/admin/ambassadors",
                    headers={"Authorization": "Bearer test-token"},
                )
        finally:
            server.API_TOKEN = old_token

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["uid"], "amb-registered")
        self.assertEqual(payload["source"], "firestore")

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

    def test_health_reports_order_inventory_capabilities(self):
        response = server.app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        features = response.get_json()["features"]
        self.assertTrue(features["perSizeInventoryReservation"])
        self.assertTrue(features["restoreInventoryOnCancellation"])

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

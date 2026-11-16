"""Browser integration smoke using real local routes/storage and fake upstreams."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import external_api
from web.routes.external_api import router

PAYMENT = "https://payments.stripe.com/upi/instructions/ui-fixture"
QR = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6lAAAAABJRU5ErkJggg=="


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class ImmediateQueue:
    def submit(self, method, *args):
        method(*args)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "web")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    outputs = Path(tempfile.mkdtemp(prefix="codex-external-api-ui-"))
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for entry, fragment in (("index.html", "external-api"), ("new.html", "/external-api")):
                calls = {"create": 0, "scan": 0}
                class Upi:
                    def __init__(self, **kwargs): pass
                    def create(self, token, **kwargs):
                        calls["create"] += 1
                        return {"ok": True, "job_id": "ui-job", "job_secret": "private-job-key", "queue_position": 0}
                    def status(self, *args):
                        return {"ok": True, "status": "done", "result": {"provider_redirect_url": PAYMENT,
                            "account_email": "ui@example.invalid", "qr_image_png": QR, "payment_status": "awaiting_payment"}}
                    def cdk_status(self): return {"enabled": True, "remaining_uses": 10, "total_uses": 20}
                class Scan:
                    def __init__(self, **kwargs): pass
                    def config(self): return {"available": True, "unit_price": "0.7", "max_batch": 200, "accepting_supplier_count": 1}
                    def orders(self, **kwargs):
                        return {"items": [{"order_no": "O-ui-fixture", "status": "completed", "link": PAYMENT, "charge_amount": "0.7"}] if calls["scan"] else [],
                                "pagination": {"total": calls["scan"]}}
                    def submit(self, links, idempotency_key):
                        calls["scan"] += 1
                        return {"batch_no": "B-ui-fixture", "order_count": len(links), "total_amount": "0.7", "balance": "9.3", "frozen_balance": "0.7", "orders": [], "idempotent": False}
                service = external_api.ExternalAPIService(outputs / (entry + ".sqlite3"), scan_factory=Scan, upi_factory=Upi, executor=ImmediateQueue())
                external_api._SERVICE = service
                app = FastAPI()
                app.include_router(router)
                api = TestClient(app)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.add_init_script("localStorage.setItem('ui-theme', 'light');")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                def handle(route):
                    request = route.request
                    from urllib.parse import urlsplit
                    parsed = urlsplit(request.url)
                    path = parsed.path
                    if path.startswith("/api/external/"):
                        result = api.request(request.method, path + ("?" + parsed.query if parsed.query else ""), content=request.post_data or b"", headers={"Content-Type": "application/json"})
                        route.fulfill(status=result.status_code, content_type="application/json", body=result.text)
                        return
                    if path == "/api/auth/status":
                        data = {"authenticated": True, "username": "admin", "ok": True}
                    elif path == "/api/status":
                        data = {"job": {"running": False}, "mail": {}, "proxy": {}, "accounts_count": 0}
                    elif path == "/api/config":
                        data = {"fingerprint": {}, "registration": {}, "mail": {"providers": []}, "proxy": {}}
                    elif path.endswith("/stream"):
                        route.fulfill(status=200, content_type="text/event-stream", body=": preview\n\n")
                        return
                    else:
                        data = {"items": [], "running": False, "total": 0, "accounts": [], "groups": [], "cards": []}
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
                page.route("**/api/**", handle)
                page.goto(f"{origin}/static/vue/{entry}#{fragment}", wait_until="domcontentloaded")
                panel = page.locator(".external-api")
                panel.wait_for()
                upi_config = panel.locator("details.ext-config").filter(has_text="UPI 提链 · 配置")
                upi_config.locator('input[type="password"]').nth(0).fill("private-cdk-fixture")
                upi_config.locator('input[type="password"]').nth(1).fill("http://user:fixture@proxy.example.test:10000")
                upi_config.locator('input[type="password"]').nth(2).fill("http://user:fixture@proxy.example.test:10000")
                upi_config.get_by_role("button", name="保存配置", exact=True).click()
                page.wait_for_function("document.querySelectorAll('details.ext-config input[type=password]')[0].value === ''")
                upi = panel.get_by_role("tabpanel", name="UPI 提链")
                upi.locator("textarea").first.fill("private-token-fixture")
                upi.get_by_role("button", name="核对计费并提链", exact=True).click()
                assert calls["create"] == 0
                upi.locator(".ext-confirm").get_by_role("button").last.click()
                page.get_by_role("button", name="填入扫码", exact=True).wait_for()
                assert calls["create"] == 1 and calls["scan"] == 0
                assert "private-job-key" not in panel.inner_text()
                page.screenshot(path=str(outputs / (entry + "-upi.png")), full_page=True)
                page.get_by_role("button", name="填入扫码", exact=True).click()
                scan_config = panel.locator("details.ext-config").filter(has_text="CDK-Scan 扫码 · 配置")
                scan_config.locator('input[type="password"]').first.fill("csk_private-fixture")
                scan_config.get_by_role("button", name="保存配置", exact=True).click()
                scan = panel.get_by_role("tabpanel", name="CDK-Scan 扫码")
                scan.get_by_role("button", name="获取报价", exact=True).click()
                assert calls["scan"] == 0
                scan.get_by_role("button", name="确认 1 条并冻结 0.7", exact=True).click()
                page.get_by_text("B-ui-fixture", exact=False).first.wait_for()
                assert calls["scan"] == 1
                page.screenshot(path=str(outputs / (entry + "-scan.png")), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "mobile layout overflow"
                page.screenshot(path=str(outputs / (entry + "-mobile.png")), full_page=True)
                assert not errors, errors
                print(entry + " settings / extraction / quote / confirmation / scan / mobile passed")
                page.close()
                api.close()
            browser.close()
        print("screenshots=" + str(outputs))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

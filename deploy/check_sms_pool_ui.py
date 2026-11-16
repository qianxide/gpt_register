"""Exercise both built SMS pool pages using local data and intercepted APIs."""
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.sms_number_pool import SmsNumberPool


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "web")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    outputs = Path(tempfile.mkdtemp(prefix="codex-sms-pool-ui-"))
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for entry, fragment in (("index.html", "sms-pool"), ("new.html", "/sms-pool")):
                pool = SmsNumberPool(outputs / (entry + ".sqlite3"))
                sms_config = {"provider": "smsbower", "enabled": True, "configured": True, "country_order": "6, 4, 151"}
                page = browser.new_page(viewport={"width": 1440, "height": 1080})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def api_route(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    path = parsed.path
                    args = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                    if path == "/api/auth/status":
                        result = {"authenticated": True, "username": "admin", "ok": True}
                    elif path == "/api/sms-pool/import":
                        result = pool.import_text(request.post_data_json["text"])
                    elif path == "/api/sms-pool" and request.method == "DELETE":
                        result = pool.delete(**request.post_data_json)
                    elif path == "/api/sms-pool":
                        result = pool.list_numbers(**args)
                    elif path == "/api/sms-config":
                        if request.method == "PUT":
                            sms_config.update(request.post_data_json)
                        result = sms_config
                    elif path == "/api/status":
                        result = {"job": {"running": False}, "mail": {}, "proxy": {}, "accounts_count": 0}
                    elif path == "/api/config":
                        result = {"fingerprint": {}, "registration": {}, "mail": {"providers": []}, "proxy": {}}
                    elif path in ("/api/groups", "/api/accounts/groups"):
                        result = {"items": []}
                    elif path.endswith("/stream"):
                        route.fulfill(status=200, content_type="text/event-stream", body=": local preview\n\n")
                        return
                    else:
                        result = {"items": [], "running": False, "total": 0, "accounts": [], "groups": [], "cards": []}
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

                page.route("**/api/**", api_route)
                page.goto(f"{base}/static/vue/{entry}#{fragment}", wait_until="domcontentloaded")
                panel = page.locator(".sms-number-pool")
                panel.wait_for()
                page.get_by_text("暂无号码，展开上方“导入号码”开始添加", exact=True).wait_for()
                panel.locator("summary").first.click()
                lines = "\n".join(f"2026-09-17|1202555{index:04d}|https://168smsapi.com/sms/openai-/ui-fixture-{index}" for index in range(101, 113))
                panel.locator("textarea").fill(lines)
                panel.get_by_role("button", name="导入号码", exact=True).click()
                panel.locator(".pool-phone").first.wait_for()
                assert panel.locator(".pool-phone").count() == 12
                panel.locator(".pool-pagination select").select_option("10")
                page.wait_for_function("document.querySelectorAll('.pool-phone').length === 10")
                panel.get_by_role("checkbox", name="全选本页", exact=True).check()
                assert panel.get_by_role("checkbox", checked=True).count() == 11
                panel.get_by_role("button", name="下一页", exact=True).click()
                page.wait_for_function("document.querySelectorAll('.pool-phone').length === 2")
                assert panel.get_by_role("checkbox", checked=True).count() == 0
                panel.get_by_role("button", name="上一页", exact=True).click()
                page.wait_for_function("document.querySelectorAll('.pool-phone').length === 10")
                panel.get_by_role("checkbox", name="全选本页", exact=True).check()
                panel.get_by_role("button", name="删除选中 (10)", exact=True).click()
                panel.get_by_role("button", name="取消", exact=True).click()
                assert pool.summary()["total"] == 12
                panel.get_by_role("button", name="删除选中 (10)", exact=True).click()
                panel.get_by_role("button", name="确认删除", exact=True).click()
                page.wait_for_function("document.querySelectorAll('.pool-phone').length === 2")
                assert pool.summary()["total"] == 2
                panel.get_by_role("button", name="使用号码池接码", exact=True).click()
                panel.get_by_role("button", name="已启用号码池接码", exact=True).wait_for()
                assert sms_config["provider"] == "imported_pool" and sms_config["enabled"]
                assert sms_config["country_order"] == "6, 4, 151"
                page.screenshot(path=str(outputs / (entry + ".png")), full_page=True)
                page.evaluate("document.documentElement.dataset.theme = 'light'")
                page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                page.screenshot(path=str(outputs / (entry + "-light.png")), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                close_nav = page.get_by_role("button", name="关闭侧栏", exact=True)
                if close_nav.count() and close_nav.is_visible():
                    close_nav.click()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Page overflow on mobile"
                page.screenshot(path=str(outputs / (entry + "-mobile.png")), full_page=True)
                assert not errors, errors
                print(entry, "import/pagination/selection/delete/activate/mobile=passed")
                page.close()
            browser.close()
        print("screenshots=" + str(outputs))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

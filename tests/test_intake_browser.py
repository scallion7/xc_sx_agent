"""Real browser: partial intake, revision, withdrawal and committed-response retry."""

import os
import unittest
from pathlib import Path

from app.scripts.demo_stack import DemoStack


@unittest.skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Enable Chromium tests")
class IntakeBrowserTests(unittest.TestCase):
    def test_partial_intake_change_withdraw_and_lost_response_retry(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(stack.url + "/after-sale")
            page.locator("#token").fill("demo-user")
            page.locator("#create").click()
            expect(page.locator("#progress")).to_contain_text("待补充资料")
            page.locator("#order").fill("ORD-20240125-006")
            lost = [False]

            def interrupt(route):
                if route.request.method == "POST" and not lost[0]:
                    lost[0] = True
                    response = route.fetch()
                    self.assertEqual(response.status, 200)
                    route.abort()
                else:
                    route.continue_()

            page.route("**/commands", interrupt)
            page.locator("#save").click()
            expect(page.locator("#status")).to_contain_text("连接中断")
            page.locator("#save").click()
            expect(page.locator("#progress")).to_contain_text("修订 1")
            expect(page.locator("#history p")).to_have_count(1)
            page.locator("#reason").fill("不合适退货退款")
            page.locator("#save").click()
            expect(page.locator("#progress")).to_contain_text("资料完整")
            page.locator("#propose").click()
            expect(page.locator("#action")).to_contain_text("确认并执行此操作")
            page.locator("#reason").fill("改为换货")
            page.locator("#caseType").select_option("exchange")
            page.locator("#save").click()
            expect(page.locator("#action")).to_be_empty()
            page.locator("#propose").click()
            expect(page.locator("#action")).to_contain_text("改为换货")
            identity = page.locator("#intakeId").input_value()
            if root := os.environ.get("BROWSER_SCREENSHOT_DIR"):
                Path(root).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(root) / "after-sale.png"), full_page=True)
            page.locator("#withdraw").click()
            expect(page.locator("#progress")).to_contain_text("申请已撤回")
            page.reload()
            page.locator("#token").fill("demo-user")
            page.locator("#intakeId").fill(identity)
            page.locator("#load").click()
            expect(page.locator("#progress")).to_contain_text("申请已撤回")
            page.locator("#token").fill("other-user")
            page.locator("#intakeId").fill(identity)
            page.locator("#load").click()
            expect(page.locator("#status")).to_contain_text("INTAKE_NOT_FOUND")
            self.assertEqual(errors, [])
            self.assertTrue(lost[0])
            browser.close()

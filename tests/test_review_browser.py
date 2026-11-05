"""Browser-driven review against real API, durable state and offline diagnostics."""

import json
import os
import unittest
from pathlib import Path

from app.scripts.demo_stack import DemoStack


@unittest.skipUnless(
    os.environ.get("RUN_BROWSER_E2E") == "1", "Enable RUN_BROWSER_E2E=1 with Chromium"
)
class ReviewBrowserTests(unittest.TestCase):
    def test_review_save_conflict_export_import_and_holdout(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1050})
            page, stale = context.new_page(), context.new_page()
            errors = []
            for tab in (page, stale):
                tab.on("pageerror", lambda error: errors.append(str(error)))
                tab.goto(stack.url + "/review")
                tab.locator("#reviewToken").fill("demo-admin")
                tab.locator("#openReview").click()
                expect(tab.locator("#progress")).to_contain_text("0 / 32")
                tab.locator("#cases button").first.click()
                expect(tab.locator("#question")).to_contain_text("衣服")
            page.locator("#documents button").first.click()
            page.locator("#approveReview").click()
            expect(page.locator("#reviewStatus")).to_contain_text(
                "REVIEW_NOTES_REQUIRED"
            )
            page.locator("#notes").fill(
                "浏览器隔离测试：八天处于十天窗口内，原文支持。"
            )
            page.locator("#approveReview").click()
            expect(page.locator("#progress")).to_contain_text("1 / 32")
            expect(page.locator("#reviewIdentity")).to_contain_text("admin")
            stale.locator("#notes").fill("旧版本修改不能覆盖新审核")
            stale.locator("#saveDraft").click()
            expect(stale.locator("#reviewStatus")).to_contain_text(
                "REVIEW_VERSION_CONFLICT"
            )
            page.locator("#loadHistory").click()
            expect(page.locator("#history")).to_contain_text("修订 1")
            page.locator("#diagnose").click()
            expect(page.locator("#reviewStatus")).to_contain_text(
                "诊断已完成", timeout=20000
            )
            expect(page.locator("#diagnostics table")).to_have_count(4)
            with page.expect_download() as downloaded:
                page.locator("#exportReview").click()
            exported = json.loads(Path(downloaded.value.path()).read_text())
            self.assertEqual(exported["reviews"][0]["reviewer"], "admin")
            self.assertFalse(exported["reviews"][1]["approved"])
            output = os.environ.get("BROWSER_SCREENSHOT_DIR")
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(output) / "review.png"), full_page=True)
                page.screenshot(path=str(Path(output) / "review-viewport.png"))
            page.get_by_text("导入已有复核文件", exact=True).click()
            page.locator("#importFile").set_input_files(
                {
                    "name": "review.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(exported).encode(),
                }
            )
            page.locator("#importReview").click()
            expect(page.locator("#reviewStatus")).to_contain_text("已导入为草稿")
            expect(page.locator("#progress")).to_contain_text("0 / 32")
            expect(page.locator("#reviewIdentity")).to_contain_text("草稿")
            page.locator("#splitFilter").select_option("holdout")
            page.locator("#cases button").first.click()
            expect(page.locator("#conditions")).to_contain_text("保留集")
            page.locator("#diagnose").click()
            expect(page.locator("#reviewStatus")).to_contain_text(
                "HOLDOUT_REVIEW_REQUIRED"
            )
            page.locator("#reviewToken").fill("demo-user")
            expect(page.locator("#documents")).to_be_empty()
            expect(page.locator("#saveDraft")).to_be_disabled()
            page.locator("#openReview").click()
            expect(page.locator("#reviewStatus")).to_contain_text("PERMISSION_DENIED")
            self.assertEqual(errors, [])
            context.close()
            browser.close()

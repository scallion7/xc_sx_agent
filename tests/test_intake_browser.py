"""Real browser: partial intake, revision, withdrawal and committed-response retry."""

import os
import unittest
from pathlib import Path

from app.scripts.demo_stack import DemoStack


@unittest.skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Enable Chromium tests")
class IntakeBrowserTests(unittest.TestCase):
    def test_revision_conflict_refresh_allows_a_new_save(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(stack.url + "/after-sale")
            page.locator("#token").fill("demo-user")
            page.locator("#create").click()
            expect(page.locator("#status")).to_contain_text("申请已创建")
            identity = page.locator("#intakeId").input_value()
            base = f"/v1/after-sale-intakes/{identity}"
            concurrent = stack.request(
                "POST",
                base + "/commands",
                json={
                    "command": "update",
                    "expected_revision": 0,
                    "fields": {"reason": "另一页面提交的原因"},
                },
                headers={"Idempotency-Key": "concurrent-update"},
            )
            self.assertEqual(concurrent.status_code, 200, concurrent.text)
            keys = []

            def record(route):
                keys.append(route.request.headers["idempotency-key"])
                route.continue_()

            page.route("**/commands", record)
            page.locator("#reason").fill("旧页面的新原因")
            page.locator("#save").click()
            expect(page.locator("#status")).to_contain_text("本次修改未保存")
            expect(page.locator("#retryPending")).to_be_disabled()
            page.locator("#refresh").click()
            expect(page.locator("#progress")).to_contain_text("修订 1")
            expect(page.locator("#save")).to_be_enabled()
            page.locator("#reason").fill("刷新后的新原因")
            page.locator("#save").click()
            expect(page.locator("#progress")).to_contain_text("修订 2")
            self.assertEqual(len(keys), 2)
            self.assertNotEqual(keys[0], keys[1])
            result = stack.request("GET", base).json()
            self.assertEqual(result["fields"]["reason"], "刷新后的新原因")
            self.assertEqual(
                len(stack.request("GET", base + "/history").json()["events"]), 2
            )
            browser.close()

    def test_unknown_result_after_reload_replays_saved_revision(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as p:
            browser = p.chromium.launch()
            # An unknown 409 must not be treated like a guaranteed CAS rejection.
            for failure in ("transport", 409, 503):
                with self.subTest(failure=failure):
                    page = browser.new_page()
                    page.goto(stack.url + "/after-sale")
                    page.locator("#token").fill("demo-user")
                    page.locator("#session").fill("unknown-" + str(failure))
                    page.locator("#create").click()
                    expect(page.locator("#status")).to_contain_text("申请已创建")
                    identity = page.locator("#intakeId").input_value()
                    base = f"/v1/after-sale-intakes/{identity}"
                    requests = []

                    def lose_ack(
                        route, _request, *, requests=requests, failure=failure
                    ):
                        requests.append(
                            (
                                route.request.headers["idempotency-key"],
                                route.request.post_data_json,
                            )
                        )
                        response = route.fetch()
                        self.assertEqual(response.status, 200)
                        if len(requests) == 1:
                            if failure == "transport":
                                route.abort()
                            else:
                                route.fulfill(
                                    status=failure,
                                    content_type="application/json",
                                    body='{"error_code":"UNKNOWN_WRITE_RESULT"}',
                                )
                        else:
                            route.fulfill(response=response)

                    page.route("**/commands", lose_ack)
                    page.locator("#reason").fill("已经提交但回包丢失的原因")
                    page.locator("#save").click()
                    expect(page.locator("#retryPending")).to_be_enabled()
                    page.reload()
                    page.locator("#token").fill("demo-user")
                    page.locator("#intakeId").fill(identity)
                    page.locator("#load").click()
                    expect(page.locator("#progress")).to_contain_text("修订 1")
                    expect(page.locator("#retryPending")).to_be_enabled()
                    if failure == "transport":
                        page.locator("#token").fill("other-user")
                        expect(page.locator("#retryPending")).to_be_disabled()
                        page.locator("#intakeId").fill(identity)
                        page.locator("#load").click()
                        expect(page.locator("#status")).to_contain_text(
                            "INTAKE_NOT_FOUND"
                        )
                        self.assertEqual(len(requests), 1)
                        page.locator("#token").fill("demo-user")
                        page.locator("#intakeId").fill(identity)
                        page.locator("#load").click()
                        expect(page.locator("#retryPending")).to_be_enabled()
                    page.locator("#reason").fill("核对后才应该提交的新原因")
                    page.locator("#save").click()
                    expect(page.locator("#status")).to_contain_text(
                        "上一条请求结果未知"
                    )
                    self.assertEqual(len(requests), 1)
                    page.locator("#retryPending").click()
                    expect(page.locator("#status")).to_contain_text("上次提交已核对")
                    expect(page.locator("#retryPending")).to_be_disabled()
                    self.assertEqual(requests[0], requests[1])
                    self.assertEqual(requests[1][1]["expected_revision"], 0)
                    self.assertEqual(stack.request("GET", base).json()["revision"], 1)
                    expect(page.locator("#history p")).to_have_count(1)
                    page.locator("#reason").fill("核对后的新原因")
                    page.locator("#save").click()
                    expect(page.locator("#progress")).to_contain_text("修订 2")
                    self.assertNotEqual(requests[1][0], requests[2][0])
                    page.close()
            browser.close()

    def test_partial_intake_change_withdraw_and_lost_response_retry(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(stack.url + "/after-sale")
            page.locator("#token").fill("demo-user")
            refresh_controls = []

            def inspect_initial_refresh(route):
                # Create renders once before its GET refresh. The form must stay
                # locked so the refresh cannot overwrite a user's new input.
                if route.request.method == "GET":
                    refresh_controls.append(
                        [
                            page.locator("#" + field).is_disabled()
                            for field in ("order", "reason", "caseType", "evidenceIds")
                        ]
                    )
                route.continue_()

            page.route("**/v1/after-sale-intakes/*", inspect_initial_refresh)
            page.locator("#create").click()
            expect(page.locator("#status")).to_contain_text("申请已创建")
            page.unroute("**/v1/after-sale-intakes/*", inspect_initial_refresh)
            self.assertTrue(refresh_controls)
            self.assertTrue(all(all(fields) for fields in refresh_controls))
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

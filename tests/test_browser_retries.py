"""Real browser + real API: lost acknowledgements reuse the original request key."""

import os
import unittest

from app.scripts.demo_stack import DemoStack


@unittest.skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Requires Chromium")
class BrowserRetryTests(unittest.TestCase):
    def test_handoff_refresh_finishes_before_human_reply_is_enabled(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as playwright:
            stack.request("GET", "/v1/sessions/delayed-handoff/ownership")
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(stack.url + "/workbench")
            page.locator("#token").fill("demo-admin")
            page.locator("#refresh").click()
            expect(page.locator("#status")).to_contain_text("待办已刷新")
            page.locator("#reply").fill("接管刷新完成后发送")
            delayed = []
            pattern = "**/v1/admin/knowledge/jobs"
            page.route(pattern, lambda route: delayed.append(route))
            takeover = []
            take_pattern = "**/conversations/user-1/delayed-handoff/take"
            page.route(take_pattern, lambda route: takeover.append(route))
            with page.expect_request(take_pattern):
                page.get_by_role("button", name="接管", exact=True).click()
            expect(page.locator("#session")).to_have_value("demo")
            for selector in ("#sendHuman", "#user", "#session", "#token", "#refresh"):
                expect(page.locator(selector)).to_be_disabled()
            self.assertEqual(len(takeover), 1)
            page.evaluate("document.getElementById('sendHuman').click()")
            self.assertEqual(
                stack.request("GET", "/v1/sessions/demo/human-messages").json()[
                    "messages"
                ],
                [],
            )
            with page.expect_request(pattern):
                takeover[0].continue_()
            page.unroute(take_pattern)
            expect(page.locator("#session")).to_have_value("delayed-handoff")
            for selector in ("#sendHuman", "#user", "#session", "#token", "#refresh"):
                expect(page.locator(selector)).to_be_disabled()
            self.assertEqual(len(delayed), 1)
            page.evaluate("document.getElementById('sendHuman').click()")
            self.assertEqual(
                stack.request(
                    "GET", "/v1/sessions/delayed-handoff/human-messages"
                ).json()["messages"],
                [],
            )
            delayed[0].continue_()
            page.unroute(pattern)
            expect(page.locator("#sendHuman")).to_be_enabled()
            page.locator("#sendHuman").click()
            expect(page.locator("#status")).to_contain_text("人工回复已发送")
            messages = stack.request(
                "GET", "/v1/sessions/delayed-handoff/human-messages"
            ).json()["messages"]
            self.assertEqual(
                [item["content"] for item in messages], ["接管刷新完成后发送"]
            )
            self.assertEqual(
                stack.request("GET", "/v1/sessions/demo/human-messages").json()[
                    "messages"
                ],
                [],
            )
            expect(page.locator("#sendHuman")).to_be_enabled()
            browser.close()

    def test_lost_run_response_survives_reload_and_reuses_key(self):
        from app.infrastructure.database import agent_runs_table, get_engine
        from playwright.sync_api import expect, sync_playwright
        from sqlalchemy import func, select

        with DemoStack() as stack, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(stack.url + "/workspace")
            page.locator("#token").fill("demo-user")
            page.locator("#session").fill("lost-response")
            page.locator("#connect").click()
            expect(page.locator("#status")).to_contain_text("会话已连接")
            keys = []
            run_ids = []

            def drop_response(route):
                keys.append(route.request.headers["idempotency-key"])
                response = route.fetch()
                self.assertEqual(response.status, 202)
                run_ids.append(response.json()["run_id"])
                route.abort("failed")

            page.route("**/v1/runs", drop_response)
            page.locator("#message").fill("你好")
            page.locator("#send").click()
            expect(page.locator("#status")).to_contain_text("结果待确认")
            page.unroute("**/v1/runs", drop_response)
            # Reload erases in-memory state but must preserve the pending intent.
            page.reload()

            def record(route):
                keys.append(route.request.headers["idempotency-key"])
                route.continue_()

            page.route("**/v1/runs", record)
            page.locator("#message").fill("你好")
            page.locator("#send").click()
            expect(page.locator("#status")).to_contain_text("已完成", timeout=20000)
            self.assertEqual(len(keys), 2)
            self.assertEqual(keys[0], keys[1])
            with get_engine(stack.database_url).connect() as conn:
                self.assertEqual(
                    conn.scalar(
                        select(func.count())
                        .select_from(agent_runs_table)
                        .where(agent_runs_table.c.session_key == "user-1:lost-response")
                    ),
                    1,
                )
            # Identity/session switches must not resume another session's run.
            page.locator("#session").fill("different")
            page.locator("#reconnect").click()
            expect(page.locator("#ownership")).to_contain_text("Agent 服务中")
            browser.close()

    def test_human_reply_double_click_and_lost_response_deliver_once(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as playwright:
            stack.request("GET", "/v1/sessions/retry-human/ownership")
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(stack.url + "/workbench")
            page.locator("#token").fill("demo-admin")
            page.locator("#refresh").click()
            expect(page.locator("#status")).to_contain_text("待办已刷新")
            page.get_by_role("button", name="接管", exact=True).click()
            expect(page.locator("#session")).to_have_value("retry-human")
            keys = []

            def drop_once(route):
                keys.append(route.request.headers["idempotency-key"])
                response = route.fetch()
                self.assertEqual(response.status, 200)
                route.abort("failed")

            pattern = "**/conversations/user-1/retry-human/messages"
            page.route(pattern, drop_once)
            page.locator("#reply").fill("重试同一条人工消息")
            page.evaluate(
                "document.getElementById('sendHuman').click();document.getElementById('sendHuman').click()"
            )
            expect(page.locator("#status")).to_contain_text("结果待确认")
            self.assertEqual(len(keys), 1)
            page.unroute(pattern, drop_once)

            def record(route):
                keys.append(route.request.headers["idempotency-key"])
                route.continue_()

            page.route(pattern, record)
            page.locator("#sendHuman").click()
            expect(page.locator("#status")).to_contain_text("人工回复已发送")
            self.assertEqual(keys[0], keys[1])
            messages = stack.request(
                "GET", "/v1/sessions/retry-human/human-messages"
            ).json()["messages"]
            self.assertEqual(len(messages), 1)
            browser.close()

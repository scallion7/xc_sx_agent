"""Real loopback processes: API + worker + model HTTP + MCP inventory, no paid LLM."""

import os
import time
import unittest
from pathlib import Path

from app.mcp_client import MCPClient
from app.platform.connector_delivery import InventoryDelivery
from app.platform.connectors import ConnectorRegistry
from app.scripts.demo_stack import DemoStack

from tests.test_platform_governance import policies


class PlatformHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = DemoStack().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.stack.__exit__(None, None, None)

    def test_real_agent_three_strategies_sse_and_trace(self):
        for strategy, mode in [
            ("react", "single"),
            ("plan_execute", "single"),
            ("react", "multi"),
        ]:
            row, headers = self.stack.run(
                "请说明七天退货政策",
                session=strategy + mode,
                strategy=strategy,
                mode=mode,
            )
            final = self.stack.completed(row["run_id"])
            self.assertEqual(final["status"], "completed", final)
            self.assertTrue(final["result"]["citations"], final)
            events = self.stack.request("GET", f"/v1/runs/{row['run_id']}/events").text
            self.assertIn("id:", events)
            last = max(
                int(line[4:]) for line in events.splitlines() if line.startswith("id: ")
            )
            resumed = self.stack.request(
                "GET",
                f"/v1/runs/{row['run_id']}/events",
                headers={"Last-Event-ID": str(last)},
            ).text
            self.assertNotIn("id:", resumed)
            trace_id = headers["traceparent"].split("-")[1]
            spans = self.stack.request(
                "GET", "/v1/admin/traces/" + trace_id, admin=True
            ).json()["spans"]
            self.assertTrue(any(x["name"] == "run.execute" for x in spans), spans)
            self.assertTrue(any(x["name"] == "model.call" for x in spans), spans)

    def test_real_mcp_confirmed_inventory(self):
        client = MCPClient(f"http://127.0.0.1:{self.stack.mcp_port}/mcp")
        definitions = client.connect()
        client.close()
        ConnectorRegistry(self.stack.config).register(
            "inventory",
            f"http://127.0.0.1:{self.stack.mcp_port}/mcp",
            definitions,
            policies(),
            "test",
        )
        response = self.stack.request(
            "POST",
            "/v1/inventory/actions",
            json={
                "session_id": "stock",
                "connector_id": "inventory",
                "tool": "reserve_inventory",
                "arguments": {"sku": "DEMO-SKU", "quantity": 2},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        action = response.json()["action"]
        response = self.stack.request(
            "POST",
            f"/v1/inventory/actions/{action['action_id']}/confirm",
            json={"session_id": "stock", "arguments_hash": action["arguments_hash"]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            row = InventoryDelivery(self.stack.config).get(
                action["action_id"], "user-1"
            )
            if row["status"] == "succeeded":
                break
            time.sleep(0.1)
        self.assertEqual(row["status"], "succeeded", row)
        self.assertEqual(row["result"]["quantity"], 2)

    def test_real_human_takeover_blocks_automatic_reply(self):
        self.stack.request("GET", "/v1/sessions/human/ownership")
        response = self.stack.request(
            "POST",
            "/v1/admin/conversations/user-1/human/take",
            admin=True,
            json={"expected_version": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        row, _ = self.stack.run("你好", session="human")
        final = self.stack.completed(row["run_id"])
        self.assertEqual(final["result"]["reply"], "")
        response = self.stack.request(
            "POST",
            "/v1/admin/conversations/user-1/human/messages",
            admin=True,
            json={"content": "人工正在协助"},
            headers={"Idempotency-Key": "message"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.stack.request("GET", "/v1/sessions/human/human-messages").json()[
                "messages"
            ][0]["content"],
            "人工正在协助",
        )

    def test_worker_process_crash_recovers_with_higher_fence(self):
        self.stack.worker.kill()
        self.stack.worker.wait()
        row, _ = self.stack.run("你好", session="crash")
        code = 'from app.repositories.runs import RunRepository; import os; r=RunRepository(os.environ["DATABASE_URL"]); c=r.claim("crashed",lease_seconds=1); assert c; os._exit(9)'
        process = self.stack.spawn("crasher", ["-c", code])
        self.assertEqual(process.wait(timeout=10), 9)
        time.sleep(1.1)
        self.stack.worker = self.stack.spawn(
            "worker-restarted", ["-m", "app.scripts.run_worker"]
        )
        final = self.stack.completed(row["run_id"])
        self.assertEqual(final["status"], "completed")
        self.assertGreaterEqual(final["attempt"], 2)


@unittest.skipUnless(
    os.environ.get("RUN_BROWSER_E2E") == "1",
    "Enable with RUN_BROWSER_E2E=1 and installed Chromium",
)
class BrowserTests(unittest.TestCase):
    def test_customer_and_operator_real_api(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            video_dir = os.environ.get("BROWSER_SCREENSHOT_DIR")
            context = browser.new_context(
                viewport={"width": 1365, "height": 1000},
                **({"record_video_dir": video_dir} if video_dir else {}),
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(stack.url + "/workspace")
            expect(page).to_have_title("Ecom Service Agent · 客户服务")
            page.locator("#token").fill("demo-user")
            page.locator("#connect").click()
            expect(page.locator("#status")).to_contain_text("会话已连接")
            page.locator("#order").fill("ORD-20240125-006")
            page.locator("#queryOrder").click()
            expect(page.locator("#orderResult")).to_contain_text("ORD-20240125-006")
            page.locator("#message").fill("请说明七天退货政策")
            page.locator("#send").click()
            expect(page.locator("#status")).to_contain_text("已完成", timeout=20000)
            page.locator("#query").fill("七天退货")
            page.locator("#search").click()
            expect(page.locator("#citations")).to_contain_text("找到可引用证据")
            page.locator("#reconnect").click()
            expect(page.locator("#conversation .bubble")).to_have_count(2)
            page.locator("#message").fill("请为订单 ORD-20240125-006 创建售后工单")
            page.locator("#send").click()
            expect(page.locator("#actions")).to_contain_text(
                "创建售后工单", timeout=20000
            )
            page.get_by_role("button", name="确认并执行", exact=True).click()
            expect(page.locator("#actions")).not_to_contain_text(
                "创建售后工单", timeout=10000
            )
            from app.infrastructure.database import after_sale_cases_table, get_engine
            from sqlalchemy import func, select

            with get_engine(stack.database_url).connect() as conn:
                self.assertEqual(
                    conn.scalar(
                        select(func.count()).select_from(after_sale_cases_table)
                    ),
                    1,
                )
            output = os.environ.get("BROWSER_SCREENSHOT_DIR")
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)
                page.screenshot(
                    path=str(Path(output) / "workspace.png"), full_page=True
                )
            admin = context.new_page()
            admin.on("pageerror", lambda error: errors.append(str(error)))
            admin.goto(stack.url + "/workbench")
            expect(admin).to_have_title("Ecom Service Agent · 客服工作台")
            admin.locator("#token").fill("demo-admin")
            admin.locator("#refresh").click()
            expect(admin.locator("#status")).to_contain_text("待办已刷新")
            admin.get_by_role("button", name="接管", exact=True).first.click()
            admin.locator("#reply").fill("人工处理完成")
            admin.locator("#sendHuman").click()
            expect(admin.locator("#status")).to_contain_text("人工回复已发送")
            page.locator("#connect").click()
            expect(page.locator("#ownership")).to_contain_text("人工服务中")
            expect(page.locator("#human")).to_contain_text("人工处理完成")
            if output:
                admin.screenshot(
                    path=str(Path(output) / "workbench.png"), full_page=True
                )
            self.assertEqual(errors, [])
            context.close()
            if video_dir:
                for tab, name in [(page, "customer-demo"), (admin, "operator-demo")]:
                    generated = Path(tab.video.path())
                    tab.video.save_as(str(Path(video_dir) / (name + ".webm")))
                    generated.unlink()
            browser.close()

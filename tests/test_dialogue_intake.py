"""Agent HTTP turns share one persistent intake and still require trusted action confirmation."""

import unittest
from uuid import uuid4

from app.scripts.demo_stack import DemoStack


class DialogueIntakeTests(unittest.TestCase):
    def test_partial_change_proposal_restart_and_no_automatic_case(self):
        with DemoStack() as stack:
            # Restart only the API/worker with the feature explicitly enabled.
            for name in ("worker", "api"):
                p = getattr(stack, name)
                p.terminate()
                p.wait(timeout=10)
            stack.env["CONVERSATION_INTAKE_ENABLED"] = "true"
            stack.api = stack.spawn("api-intake", ["-m", "app.scripts.run_api"])
            stack.wait(stack.url + "/ready")
            stack.worker = stack.spawn(
                "worker-intake", ["-m", "app.scripts.run_worker"]
            )
            for message in (
                "售后申请",
                "补充订单 ORD-20240125-006",
                "补充原因：尺寸不合适",
                "生成提案",
            ):
                row, _ = stack.run(message, session="dialogue", request_id=uuid4().hex)
                final = stack.completed(row["run_id"])
                self.assertEqual(final["status"], "completed", final)
            actions = stack.request("GET", "/v1/sessions/dialogue/actions").json()[
                "actions"
            ]
            self.assertEqual(len(actions), 1, actions)
            old = actions[0]
            self.assertEqual(old["status"], "pending")
            row, _ = stack.run("改为换货", session="dialogue", request_id=uuid4().hex)
            stack.completed(row["run_id"])
            blocked = stack.request(
                "POST",
                f"/v1/actions/{old['action_id']}/confirm",
                json={
                    "session_id": "dialogue",
                    "arguments_hash": old["arguments_hash"],
                },
            )
            self.assertEqual(blocked.status_code, 409)
            stack.worker.terminate()
            stack.worker.wait(timeout=10)
            stack.worker = stack.spawn(
                "worker-resumed", ["-m", "app.scripts.run_worker"]
            )
            row, _ = stack.run("生成提案", session="dialogue", request_id=uuid4().hex)
            stack.completed(row["run_id"])
            from app.infrastructure.database import after_sale_cases_table, get_engine
            from app.platform.after_sale_intake import TABLE, AfterSaleIntake
            from app.web.services import build_api_services
            from sqlalchemy import func, select

            with get_engine(stack.database_url).connect() as conn:
                iid = conn.scalar(
                    select(TABLE.c.intake_id).where(TABLE.c.session_id == "dialogue")
                )
                self.assertEqual(
                    conn.scalar(
                        select(func.count()).select_from(after_sale_cases_table)
                    ),
                    0,
                )
            flow = AfterSaleIntake(build_api_services(stack.config), "user-1").get(iid)
            self.assertEqual(flow["fields"]["order_id"], "ORD-20240125-006")
            self.assertEqual(flow["fields"]["case_type"], "exchange")
            self.assertIn("run_id", flow["field_sources"]["reason"])
            self.assertNotEqual(flow["action_id"], old["action_id"])

            current = flow["action"]
            response = stack.request(
                "POST",
                f"/v1/actions/{current['action_id']}/confirm",
                json={
                    "session_id": "dialogue",
                    "arguments_hash": current["arguments_hash"],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            repeated = stack.request(
                "POST",
                f"/v1/actions/{current['action_id']}/confirm",
                json={
                    "session_id": "dialogue",
                    "arguments_hash": current["arguments_hash"],
                },
            )
            self.assertEqual(repeated.status_code, 200, repeated.text)
            for _ in range(2):
                executed = stack.request(
                    "POST",
                    f"/v1/actions/{current['action_id']}/execute",
                    json={"session_id": "dialogue"},
                )
                self.assertEqual(executed.status_code, 200, executed.text)
                self.assertTrue(executed.json().get("success"), executed.text)
            with get_engine(stack.database_url).connect() as conn:
                self.assertEqual(
                    conn.scalar(
                        select(func.count()).select_from(after_sale_cases_table)
                    ),
                    1,
                )
            for message in ("新申请", "撤回申请"):
                row, _ = stack.run(message, session="dialogue", request_id=uuid4().hex)
                self.assertEqual(stack.completed(row["run_id"])["status"], "completed")
            listing = stack.request(
                "GET", "/v1/sessions/dialogue/after-sale-intakes"
            ).json()["intakes"]
            self.assertEqual(len(listing), 2)
            self.assertEqual(listing[0]["status"], "withdrawn")
            other = stack.request(
                "GET",
                "/v1/sessions/dialogue/after-sale-intakes",
                headers={"X-API-Key": "other-user"},
            ).json()
            self.assertEqual(other["intakes"], [])

"""Policy safety is independent of result limits; real Agent turns retain sourced scope."""

import copy
import json
from types import SimpleNamespace

from app.agent.chat import EcomAgent
from app.agent.tools.contracts import ProductArguments
from app.agent.tools.definitions import definitions_by_name
from app.agent.tools.registry import RegisteredTool
from app.multi_agent.agents import SubAgent
from app.observability import EventBus
from app.platform.retrieval import HybridRetrieval
from app.repositories.mock_commerce import MockCommerceRepository
from app.schemas.response import CustomerServiceResponse, IntentType
from app.services.commerce import CommerceService

from tests.test_knowledge_platform import PlatformTestCase


class PolicyConflictTests(PlatformTestCase):
    def test_conflict_outside_candidate_pool_cannot_be_hidden_by_top_k(self):
        self.publish(
            [
                self.ingest(
                    "allow",
                    text="服饰签收七天退货。",
                    policy_key="returns",
                    decision="allow",
                ),
                self.ingest(
                    "deny", text="另见审批附录。", policy_key="returns", decision="deny"
                ),
            ]
        )
        for compiled in (False, True):
            retriever = HybridRetrieval(
                self.config.model_copy(update={"compiled_retrieval_enabled": compiled})
            )
            for k in (1, 2, 5):
                with self.subTest(compiled=compiled, k=k):
                    result = retriever.diagnose(
                        "服饰签收七天退货", mode="bm25", top_k=k
                    )
                    self.assertEqual(result["outcome"], "policy_conflict")
                    self.assertFalse(result["grounded"])
                    self.assertEqual(
                        result["diagnostics"]["conflicting_policy_keys"], ["returns"]
                    )
        self.k.delete_document("deny", 1, "admin")
        self.assertEqual(
            HybridRetrieval(self.config).search("服饰签收七天退货", top_k=1)["outcome"],
            "answerable",
        )

    def test_unrelated_expired_future_and_inapplicable_decisions_do_not_conflict(self):
        versions = [
            self.ingest(
                "allow", text="服饰七天退货。", policy_key="returns", decision="allow"
            )
        ]
        for name, metadata in [
            ("expired", {"expires_at": "2021-01-01"}),
            ("future", {"effective_date": "2099-01-01"}),
            ("other-region", {"regions": ["北京"]}),
            ("other-channel", {"channels": ["商城"]}),
            ("other-product", {"products": ["家电"]}),
            ("unrelated", {"policy_key": "warranty"}),
        ]:
            versions.append(
                self.ingest(
                    name,
                    text="另见审批附录。",
                    **({"policy_key": "returns", "decision": "deny"} | metadata),
                )
            )
        self.publish(versions)
        result = HybridRetrieval(self.config).search(
            "服饰七天退货",
            top_k=1,
            day="2026-11-10",
            context={"region": "上海", "channel": "门店", "product": "服饰"},
        )
        self.assertEqual(result["outcome"], "answerable")

    def test_related_unknown_scope_requires_clarification_even_outside_top_k(self):
        self.publish(
            [
                self.ingest(
                    "allow",
                    text="服饰七天退货。",
                    policy_key="returns",
                    decision="allow",
                ),
                self.ingest(
                    "deny",
                    text="另见审批附录。",
                    policy_key="returns",
                    decision="deny",
                    regions=["上海"],
                ),
            ]
        )
        result = HybridRetrieval(self.config).search("服饰七天退货", top_k=1)
        self.assertEqual(result["outcome"], "needs_clarification")


class PolicyGateway:
    """Controlled model chooses one tool; actual Agent/tool/retrieval/session code runs."""

    client = None

    def __init__(self, arguments, name="search_knowledge"):
        self.arguments = arguments if isinstance(arguments, list) else [arguments]
        self.name = name
        self.calls = 0
        self.observed_tools = []

    def complete(self, **kwargs):
        self.observed_tools.extend(
            json.loads(m["content"]) for m in kwargs["messages"] if m["role"] == "tool"
        )
        self.calls += 1
        call = SimpleNamespace(
            id="policy-call",
            function=SimpleNamespace(
                name=self.name,
                arguments=json.dumps(
                    self.arguments[min(self.calls - 1, len(self.arguments) - 1)],
                    ensure_ascii=False,
                ),
            ),
        )
        message = SimpleNamespace(
            content="服饰签收七天内可以退货。",
            tool_calls=[call] if self.calls <= len(self.arguments) else None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def parse(self, **kwargs):
        parsed = CustomerServiceResponse(
            intent=IntentType.AFTER_SALE,
            confidence=1,
            reply=kwargs["messages"][-1]["content"],
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )


class ScopedOrders(MockCommerceRepository):
    def __init__(self):
        self.orders = {
            "ORD-OWNED": {
                "order_id": "ORD-OWNED",
                "owner_id": "user-1",
                "policy_context": {
                    "region": "上海",
                    "channel": "门店",
                    "product": "服饰",
                },
            },
            "ORD-EMPTY": {"order_id": "ORD-EMPTY", "owner_id": "user-1"},
            "ORD-FOREIGN": {
                "order_id": "ORD-FOREIGN",
                "owner_id": "user-2",
                "policy_context": {
                    "region": "上海",
                    "channel": "门店",
                    "product": "服饰",
                },
            },
        }

    def get_order(self, order_id):
        return copy.deepcopy(self.orders.get(order_id))


class PolicyDialogueTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.publish(
            [
                self.ingest(
                    text="服饰签收七天内可以退货。",
                    regions=["上海"],
                    channels=["门店"],
                    products=["服饰"],
                )
            ]
        )
        self.commerce = CommerceService(ScopedOrders())

    def turn(
        self, message, arguments=None, *, user="user-1", session="policy-dialogue"
    ):
        agent = EcomAgent(
            config=self.config.model_copy(
                update={"memory_user_id": user, "history_threshold": 100}
            ),
            session_path=session,
            model_gateway=PolicyGateway(
                [
                    {"query": "服饰签收七天退货"} | args
                    for args in (
                        arguments if isinstance(arguments, list) else [arguments or {}]
                    )
                ]
            ),
            commerce_service=self.commerce,
        )
        try:
            response = agent.chat(message)
            tool = next(
                json.loads(m["content"])
                for m in reversed(agent.raw_messages)
                if m["role"] == "tool"
            )
            state = copy.deepcopy(agent._runtime_state.get("knowledge_context", {}))
            return response, tool, state
        finally:
            agent.release_resources()

    def test_clarification_completes_across_real_agent_turns_and_restart(self):
        first, _, _ = self.turn("服饰签收七天能退货吗？")
        self.assertEqual(first.evidence_state, "needs_clarification")
        self.assertTrue(first.requires_human)
        second, tool, state = self.turn(
            "补充地域上海、购买渠道门店、商品服饰。",
            {"region": "上海", "channel": "门店", "product": "服饰"},
        )
        self.assertEqual(second.evidence_state, "answerable")
        self.assertFalse(second.requires_human)
        self.assertEqual(second.reply, "服饰签收七天内可以退货。")
        self.assertEqual(
            tool["context"], {"region": "上海", "channel": "门店", "product": "服饰"}
        )
        self.assertTrue(
            all(
                s["origin"] == "user_message" and s["message_sha256"]
                for s in state["sources"].values()
            )
        )
        third, _, _ = self.turn("再核实一下这个退货政策。")
        self.assertEqual(third.evidence_state, "answerable")
        other, _, _ = self.turn("服饰能退货吗？", user="user-2")
        self.assertEqual(other.evidence_state, "needs_clarification")

    def test_model_cannot_invent_conditions_or_use_negated_user_values(self):
        for message in ("服饰能退货吗？", "不是上海，先核实服饰退货政策。"):
            response, tool, state = self.turn(
                message, {"region": "上海", "channel": "门店", "product": "服饰"}
            )
            self.assertTrue(response.requires_human)
            self.assertEqual(tool["error_code"], "KNOWLEDGE_CONTEXT_SOURCE_REQUIRED")
            self.assertFalse(state)

    def test_owned_order_facts_are_authoritative_and_switch_clears_context(self):
        response, tool, _ = self.turn(
            "查询订单 ORD-OWNED 的退货政策。", {"order_id": "ORD-OWNED"}
        )
        self.assertEqual(response.evidence_state, "answerable")
        self.assertTrue(
            all(
                s == {"origin": "owned_order", "order_id": "ORD-OWNED"}
                for s in tool["context_sources"].values()
            )
        )
        conflicting, tool, _ = self.turn("我说的是北京。", {"region": "北京"})
        self.assertTrue(conflicting.requires_human)
        self.assertEqual(tool["error_code"], "KNOWLEDGE_CONTEXT_CONFLICT")
        changed, tool, _ = self.turn(
            "换成订单 ORD-EMPTY 核实退货政策。", {"order_id": "ORD-EMPTY"}
        )
        self.assertEqual(changed.evidence_state, "needs_clarification")
        self.assertEqual(tool["context"], {})

    def test_foreign_or_unselected_orders_cannot_supply_scope(self):
        for message, code in [
            ("查询订单 ORD-FOREIGN 的政策。", "KNOWLEDGE_ORDER_UNAVAILABLE"),
            ("服饰能退货吗？", "KNOWLEDGE_CONTEXT_SOURCE_REQUIRED"),
        ]:
            response, tool, state = self.turn(message, {"order_id": "ORD-FOREIGN"})
            self.assertTrue(response.requires_human)
            self.assertEqual(tool["error_code"], code)
            self.assertFalse(state)

    def test_negated_previous_condition_is_not_reused_when_model_omits_it(self):
        self.turn(
            "上海门店购买的服饰。",
            {"region": "上海", "channel": "门店", "product": "服饰"},
        )
        response, tool, state = self.turn("不是上海，是北京。")
        self.assertEqual(response.evidence_state, "needs_clarification")
        self.assertNotIn("region", tool["context"])
        self.assertNotIn("region", state["fields"])

    def test_changed_order_is_not_reused_when_model_omits_selection(self):
        self.turn("查询订单 ORD-OWNED 的退货政策。", {"order_id": "ORD-OWNED"})
        response, tool, state = self.turn("换成订单 ORD-EMPTY 核实退货政策。")
        self.assertTrue(response.requires_human)
        self.assertEqual(tool["error_code"], "KNOWLEDGE_ORDER_SELECTION_REQUIRED")
        self.assertNotIn("order_id", state)
        # A genuinely new user turn may repeat the same words to clarify.
        response, tool, _ = self.turn(
            "换成订单 ORD-EMPTY 核实退货政策。", {"order_id": "ORD-EMPTY"}
        )
        self.assertEqual(response.evidence_state, "needs_clarification")
        self.assertEqual(tool["context"], {})

    def test_model_retry_cannot_omit_conflicting_field_to_revive_order_scope(self):
        self.turn("查询订单 ORD-OWNED 的退货政策。", {"order_id": "ORD-OWNED"})
        response, tool, state = self.turn("我说的是北京。", [{"region": "北京"}, {}])
        self.assertTrue(response.requires_human)
        self.assertEqual(tool["error_code"], "KNOWLEDGE_CONTEXT_RECONFIRM_REQUIRED")
        self.assertNotIn("order_id", state)

    def test_tool_json_keeps_types_redaction_and_action_references_on_replay(self):
        references = {
            "action_id": "a" * 21 + "13800138000",
            "arguments_hash": "b" * 48 + "6222021234567890",
            "business_version": "c" * 48 + "6222021234567890",
        }
        payload = {
            "success": True,
            "score": 0.03278688524590164,
            "note": "电话13800138000 邮箱 person@example.com 卡号6222021234567890",
            "action": references,
        }
        gateway = PolicyGateway({"keyword": "服饰"}, name="query_product")
        config = self.config.model_copy(
            update={"memory_user_id": "user-1", "history_threshold": 100}
        )
        agent = EcomAgent(
            config=config, session_path="json-replay", model_gateway=gateway
        )
        self.addCleanup(agent.release_resources)
        agent.tool_registry.register(
            RegisteredTool(
                definition=definitions_by_name()["query_product"],
                handler=lambda keyword: payload,
                arguments_model=ProductArguments,
            )
        )
        agent.chat("查询服饰商品。")
        expected = payload | {"note": "电话[PHONE] 邮箱 [EMAIL] 卡号[BANK_CARD]"}
        self.assertEqual(gateway.observed_tools[-1], expected)
        self.assertEqual(
            json.loads(
                next(m["content"] for m in agent.raw_messages if m["role"] == "tool")
            ),
            expected,
        )

        restored = EcomAgent(
            config=config,
            session_path="json-replay",
            model_gateway=PolicyGateway({"query": "服饰退货"}),
        )
        self.addCleanup(restored.release_resources)
        for messages in (restored.raw_messages, restored.messages):
            self.assertEqual(
                json.loads(next(m["content"] for m in messages if m["role"] == "tool")),
                expected,
            )

        # Multi-Agent uses the identical real execution service and must not
        # damage its serialized result before the next model call either.
        multi_gateway = PolicyGateway({"keyword": "服饰"}, name="query_product")
        subagent = SubAgent(
            "test",
            "test",
            agent.tool_manager,
            multi_gateway,
            "offline",
            0,
            EventBus(),
            agent.guardrails.redact,
        )
        _, messages = subagent.handle([{"role": "user", "content": "查询服饰"}])
        self.assertEqual(multi_gateway.observed_tools[-1], expected)
        self.assertEqual(
            json.loads(next(m["content"] for m in messages if m["role"] == "tool")),
            expected,
        )

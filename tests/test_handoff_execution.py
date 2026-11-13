"""Ownership generations fence queued and in-flight runs without replaying old intent."""

from unittest.mock import Mock, patch

from app.agent.chat import EcomAgent
from app.platform.conversation_handoff import ConversationHandoffService
from app.repositories.runs import RunError, RunInterrupted
from app.repositories.session import SqlSessionRepository
from app.services.run_context import current_run
from app.services.runs import RunWorker
from app.web.services import build_api_services, runtime_snapshot

from tests.fakes import FakeOpenAI
from tests.test_async_runs import AsyncRunTests, FakeAgent, reply


class HandoffExecutionTests(AsyncRunTests):
    # Do not inherit baseline tests a second time: overridden below via load_tests.
    def setup_handoff(self, factory):
        self.config = self.config.model_copy(update={"collaboration_enabled": True})
        self.services = build_api_services(self.config, agent_factory=factory)
        self.handoff = ConversationHandoffService(self.config)
        self.handoff.ensure("user-1", "user-1:chat")

    def submit_owned(self):
        return self.submit(
            runtime_snapshot=runtime_snapshot(self.services, "user-1", "user-1:chat")
        )

    def test_owned_queue_skips_factory_and_preserves_user_message(self):
        factory = Mock(return_value=FakeAgent())
        self.setup_handoff(factory)
        self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)
        row = self.submit_owned()
        RunWorker(self.services, heartbeat=False).run_once()
        final = self.repo.get(row["run_id"])
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(final["payload"]["result"]["evidence_state"], "awaiting_human")
        factory.assert_not_called()
        self.assertEqual(
            SqlSessionRepository(self.url, "user-1:chat", "user-1").load()["messages"],
            [{"role": "user", "content": "你好"}],
        )
        self.assertEqual(final["payload"]["calls"], {})

    def test_take_release_invalidates_old_run_new_request_executes(self):
        factory = Mock(return_value=FakeAgent())
        self.setup_handoff(factory)
        row = self.submit_owned()
        self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)
        self.handoff.transition("user-1", "user-1:chat", "operator", 2, take=False)
        worker = RunWorker(self.services, heartbeat=False)
        worker.run_once()
        factory.assert_not_called()
        self.assertTrue(
            self.repo.get(row["run_id"])["payload"]["handoff"]["requires_new_request"]
        )
        fresh = self.submit(
            key="fresh",
            runtime_snapshot=runtime_snapshot(self.services, "user-1", "user-1:chat"),
        )
        worker.run_once()
        factory.assert_called_once()
        self.assertEqual(
            self.repo.get(fresh["run_id"])["payload"]["result"]["reply"], "完成"
        )

    def test_in_flight_call_finishes_but_next_dispatch_is_blocked(self):
        outer = self
        dispatched = []

        class Agent(FakeAgent):
            def chat(self, message):
                def first():
                    dispatched.append("model")
                    outer.handoff.transition(
                        "user-1", "user-1:chat", "operator", 1, take=True
                    )
                    outer.handoff.transition(
                        "user-1", "user-1:chat", "operator", 2, take=False
                    )
                    return {"usage": 30}

                run = current_run()
                run.call("model", "controlled", {}, first)
                run.call("read", "query_order", {}, lambda: dispatched.append("read"))
                return reply()

        self.setup_handoff(lambda **_: Agent())
        row = self.submit_owned()
        RunWorker(self.services, heartbeat=False).run_once()
        final = self.repo.get(row["run_id"])
        self.assertEqual(dispatched, ["model"])
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(final["payload"]["result"]["reply"], "")
        self.assertEqual(len(final["payload"]["calls"]), 1)
        self.assertEqual(
            next(iter(final["payload"]["calls"].values()))["state"], "completed"
        )

    def test_dispatch_transaction_closes_guard_check_race(self):
        from unittest.mock import patch

        from app.repositories.runs import RunInterrupted
        from app.services.run_context import RunContext

        self.setup_handoff(lambda **_: FakeAgent())
        self.submit_owned()
        context = RunContext(self.repo, self.repo.claim("test"))
        operation = Mock()

        def after_check():
            self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)

        with patch.object(context, "guard_ownership", side_effect=after_check):
            with self.assertRaisesRegex(RunInterrupted, "RUN_HANDOFF"):
                context.call("model", "controlled", {}, operation)
        operation.assert_not_called()
        self.assertEqual(self.repo.get(context.run_id)["payload"]["calls"], {})

    def test_final_publish_suppresses_reply_after_take_and_release(self):
        self.setup_handoff(lambda **_: FakeAgent())
        self.submit_owned()
        claimed = self.repo.claim("test")
        self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)
        self.handoff.transition("user-1", "user-1:chat", "operator", 2, take=False)
        final = self.repo.change(
            claimed,
            {
                "status": "completed",
                "payload": claimed["payload"]
                | {"result": reply().model_dump(mode="json")},
            },
            "run.completed",
        )
        self.assertEqual(final["payload"]["result"]["reply"], "")
        self.assertTrue(final["payload"]["result"]["requires_human"])

    def test_real_agent_saved_reply_and_user_input_commit_only_handoff(self):
        def factory(**kwargs):
            kwargs.pop("mode")
            return EcomAgent(client=FakeOpenAI(), **kwargs)

        self.setup_handoff(factory)
        row = self.submit_owned()
        actual = EcomAgent.save

        def take_after_save(agent):
            actual(agent)
            self.assertIsNotNone(current_run().session_output)
            self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)

        with patch.object(EcomAgent, "save", take_after_save):
            RunWorker(self.services, heartbeat=False).run_once()
        final = self.repo.get(row["run_id"])
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["payload"]["internal_draft"]["reply"])
        self.assertEqual(final["payload"]["result"]["evidence_state"], "awaiting_human")
        self.assertEqual(
            SqlSessionRepository(self.url, "user-1:chat", "user-1").load()["messages"],
            [{"role": "user", "content": "你好"}],
        )

    def test_interrupted_handoff_rolls_back_and_next_owner_appends_once(self):
        self.setup_handoff(lambda **_: FakeAgent())
        self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)
        row = self.submit_owned()
        sessions = SqlSessionRepository(self.url, "user-1:chat", "user-1")
        worker = RunWorker(self.services, repository=self.repo, heartbeat=False)
        actual = self.repo._event

        def after_event(conn, run, event_type, detail):
            actual(conn, run, event_type, detail)
            if event_type == "run.completed":
                raise SystemExit(9)

        with patch.object(self.repo, "_event", side_effect=after_event):
            with self.assertRaises(SystemExit):
                worker.run_once()
        self.assertIsNone(sessions.load())
        self.assertFalse(
            any(
                e["event_type"] == "run.handoff"
                for e in self.repo.events(row["run_id"])
            )
        )
        self.expire(row["run_id"])
        worker.run_once()
        self.assertEqual(
            sessions.load()["messages"], [{"role": "user", "content": "你好"}]
        )
        self.assertEqual(
            [e["event_type"] for e in self.repo.events(row["run_id"])][-2:],
            ["run.handoff", "run.completed"],
        )
        self.submit(key="new")

    def test_legacy_partial_handoff_is_recognized_without_duplicate_or_overwrite(self):
        self.setup_handoff(lambda **_: FakeAgent())
        self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)
        row = self.submit_owned()
        self.repo.claim("old")
        from app.services.run_context import RunContext

        sessions = SqlSessionRepository(self.url, "user-1:chat", "user-1")
        with RunContext(self.repo, self.repo.get(row["run_id"])):
            sessions.load()
        # Old worker independently committed the exact snapshot + user input.
        customer = [{"role": "user", "content": "你好"}]
        sessions.save(customer, None, None)
        self.expire(row["run_id"])
        RunWorker(self.services, repository=self.repo, heartbeat=False).run_once()
        self.assertEqual(self.repo.get(row["run_id"])["status"], "completed")
        self.assertEqual(sessions.load()["messages"], customer)

    def test_legacy_handoff_preserves_conflicting_external_history(self):
        self.setup_handoff(lambda **_: FakeAgent())
        self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)
        row = self.submit_owned()
        from app.services.run_context import RunContext

        context = RunContext(self.repo, self.repo.claim("old"))
        sessions = SqlSessionRepository(self.url, "user-1:chat", "user-1")
        with context:
            sessions.load()
        external = [{"role": "user", "content": "different committed history"}]
        sessions.save(external, None, None)
        self.expire(row["run_id"])
        RunWorker(self.services, repository=self.repo, heartbeat=False).run_once()
        self.assertEqual(self.repo.get(row["run_id"])["status"], "needs_reconciliation")
        self.assertEqual(sessions.load()["messages"], external)
        self.assertFalse(
            any(
                e["event_type"] == "run.handoff"
                for e in self.repo.events(row["run_id"])
            )
        )

    def test_expired_owner_cannot_commit_handoff_at_final_publish(self):
        self.setup_handoff(lambda **_: FakeAgent())
        self.handoff.transition("user-1", "user-1:chat", "operator", 1, take=True)
        row = self.submit_owned()
        claimed = self.repo.claim("old")
        self.expire(row["run_id"])
        with self.assertRaises((RunInterrupted, RunError)) as error:
            self.repo.change(
                claimed,
                {"status": "completed", "payload": claimed["payload"]},
                "run.completed",
                handoff=True,
            )
        self.assertIn(error.exception.code, ("RUN_FENCED", "RUN_VERSION_CONFLICT"))
        self.assertIsNone(
            SqlSessionRepository(self.url, "user-1:chat", "user-1").load()
        )


def load_tests(loader, tests, pattern):
    import unittest

    names = [n for n in HandoffExecutionTests.__dict__ if n.startswith("test_")]
    return unittest.TestSuite(HandoffExecutionTests(n) for n in names)

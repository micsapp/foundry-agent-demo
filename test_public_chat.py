"""Public main-agent routing and approval lifecycle without Azure credentials."""

import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from azure.core.exceptions import ResourceExistsError
from azure_function.public_chat import PublicChat, RateLimiter


TOKEN = "a" * 64
OTHER_TOKEN = "b" * 64


def response(response_id="resp_initial", approvals=(), text=""):
    return SimpleNamespace(
        id=response_id, output_text=text, usage=None,
        output=[SimpleNamespace(type="mcp_approval_request", id=name,
                                name="update_oracle_incident", arguments='{"incident_id":"INC-1042","new_status":"RESOLVED"}')
                for name in approvals],
    )


class PublicChatTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "PUBLIC_CHAT_AGENT": "Oracle-And-KB-Guarded-Agent", "PUBLIC_CHAT_AGENT_VERSION": "13",
            "CHAT_VISITOR_SALT": "test-salt", "CHAT_DAILY_LIMIT": "100", "CHAT_PER_VISITOR_PER_HOUR": "20",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = Mock()
        self.conversations = {}
        self.responses = {}
        self.client.conversations.create.side_effect = self.create_conversation
        self.client.conversations.retrieve.side_effect = lambda cid: SimpleNamespace(
            id=cid, metadata=copy.deepcopy(self.conversations[cid]),
        )
        self.client.conversations.update.side_effect = self.update_conversation
        self.client.responses.retrieve.side_effect = lambda rid: self.responses[rid]
        self.client.responses.create.side_effect = self.create_response
        self.next_response = response(text="Hello")
        self.limiter = Mock()
        self.limiter.take.return_value = True
        self.claims = set()
        self.limiter.claim_approval.side_effect = self.claim
        self.chat = PublicChat(lambda: self.client, lambda: self.limiter)

    def create_conversation(self, *, metadata):
        cid = f"conv_{len(self.conversations):012d}"
        self.conversations[cid] = copy.deepcopy(metadata)
        return SimpleNamespace(id=cid)

    def update_conversation(self, cid, *, metadata):
        self.conversations[cid] = copy.deepcopy(metadata)

    def create_response(self, **kwargs):
        result = self.next_response
        self.responses[result.id] = result
        return result

    def claim(self, cid, rid):
        key = (cid, rid)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    def post(self, **data):
        data.setdefault("session_token", TOKEN)
        return self.chat.handle(json.dumps(data).encode(), {"x-forwarded-for": "192.0.2.1"})

    def pending(self, ids=("approval_1",)):
        self.next_response = response(approvals=ids)
        status, data = self.post(message="Resolve INC-1042")
        self.assertEqual(status, 200, data)
        return data

    def test_routes_to_exact_main_agent_version(self):
        status, data = self.post(message="Hello")
        self.assertEqual(status, 200)
        self.assertEqual(data["agent"], {"name": "Oracle-And-KB-Guarded-Agent", "version": "13"})
        self.assertEqual(self.client.responses.create.call_args.kwargs["extra_body"], {
            "agent_reference": {"name": "Oracle-And-KB-Guarded-Agent", "type": "agent_reference", "version": "13"},
        })

    def test_write_pauses_and_only_explicit_approval_continues(self):
        pending = self.pending()
        self.assertEqual(pending["approvals"][0]["arguments"]["incident_id"], "INC-1042")
        self.assertEqual(self.client.responses.create.call_count, 1)
        # Typing "approve" cannot bypass the review cards.
        status, recovered = self.post(message="approve", conversation_id=pending["conversation_id"])
        self.assertEqual(status, 200)
        self.assertEqual(recovered["approvals"], pending["approvals"])
        self.assertEqual(self.client.responses.create.call_count, 1)
        self.next_response = response("resp_done", text="Updated")
        status, result = self.post(conversation_id=pending["conversation_id"], approvals=[
            {"approval_request_id": "approval_1", "approve": True, "arguments": "ignored client data"},
        ])
        self.assertEqual(status, 200, result)
        self.assertEqual(result["approvals"], [])
        self.assertEqual(self.client.responses.create.call_args.kwargs["input"], [
            {"type": "mcp_approval_response", "approval_request_id": "approval_1", "approve": True},
        ])
        self.assertEqual(self.conversations[pending["conversation_id"]]["pending_response"], "")

    def test_rejection_is_sent_to_foundry(self):
        pending = self.pending()
        self.next_response = response("resp_rejected", text="No change made")
        status, data = self.post(conversation_id=pending["conversation_id"], approvals=[
            {"approval_request_id": "approval_1", "approve": False},
        ])
        self.assertEqual(status, 200, data)
        self.assertIs(self.client.responses.create.call_args.kwargs["input"][0]["approve"], False)

    def test_other_browser_cannot_approve_or_continue_conversation(self):
        pending = self.pending()
        status, _ = self.post(session_token=OTHER_TOKEN, conversation_id=pending["conversation_id"], approvals=[
            {"approval_request_id": "approval_1", "approve": True},
        ])
        self.assertEqual(status, 403)
        self.assertEqual(self.client.responses.create.call_count, 1)
        self.next_response = response("resp_other", text="New chat")
        status, data = self.post(session_token=OTHER_TOKEN, message="Hello", conversation_id=pending["conversation_id"])
        self.assertEqual(status, 200)
        self.assertNotEqual(data["conversation_id"], pending["conversation_id"])

    def test_target_change_invalidates_old_approval(self):
        pending = self.pending()
        with patch.dict(os.environ, {"PUBLIC_CHAT_AGENT_VERSION": "14"}):
            status, _ = self.post(conversation_id=pending["conversation_id"], approvals=[
                {"approval_request_id": "approval_1", "approve": True},
            ])
        self.assertEqual(status, 403)
        self.assertEqual(self.client.responses.create.call_count, 1)

    def test_all_pending_ids_must_match_exactly(self):
        pending = self.pending(("approval_1", "approval_2"))
        for ids in (("approval_1",), ("approval_1", "forged"), ("approval_1", "approval_1")):
            with self.subTest(ids=ids):
                status, _ = self.post(conversation_id=pending["conversation_id"], approvals=[
                    {"approval_request_id": name, "approve": True} for name in ids
                ])
                self.assertEqual(status, 400)
        self.assertEqual(self.client.responses.create.call_count, 1)
        self.next_response = response("resp_both", text="Decisions received")
        status, _ = self.post(conversation_id=pending["conversation_id"], approvals=[
            {"approval_request_id": "approval_1", "approve": True},
            {"approval_request_id": "approval_2", "approve": False},
        ])
        self.assertEqual(status, 200)

    def test_replays_and_concurrent_submissions_do_not_execute_twice(self):
        pending = self.pending()
        self.claims.add((pending["conversation_id"], "resp_initial"))
        status, _ = self.post(conversation_id=pending["conversation_id"], approvals=[
            {"approval_request_id": "approval_1", "approve": True},
        ])
        self.assertEqual(status, 409)
        self.assertEqual(self.client.responses.create.call_count, 1)

    def test_invalid_body_cannot_reach_foundry(self):
        for data in (
            {"message": "Hello", "session_token": "bad"},
            {"approvals": [{"approval_request_id": "a", "approve": "true"}]},
            {"approvals": [{"approval_request_id": "a", "approve": 1}]},
            {"approvals": []},
            {"message": "Hello", "approvals": [{"approval_request_id": "a", "approve": True}]},
        ):
            with self.subTest(data=data):
                status, _ = self.post(**data)
                self.assertEqual(status, 400)
        self.client.responses.create.assert_not_called()

    def test_daily_limit_blocks_approval_before_claim(self):
        pending = self.pending()
        self.limiter.take.side_effect = lambda partition, *args: not partition.startswith("day-")
        status, _ = self.post(conversation_id=pending["conversation_id"], approvals=[
            {"approval_request_id": "approval_1", "approve": True},
        ])
        self.assertEqual(status, 429)
        self.limiter.claim_approval.assert_not_called()
        self.assertEqual(self.client.responses.create.call_count, 1)

    def test_azure_table_claim_is_atomic(self):
        table = Mock()
        table.create_entity.side_effect = [None, ResourceExistsError("Already submitted")]
        limiter = RateLimiter(table)
        self.assertTrue(limiter.claim_approval("conv_test", "resp_test"))
        self.assertFalse(limiter.claim_approval("conv_test", "resp_test"))


if __name__ == "__main__":
    unittest.main()

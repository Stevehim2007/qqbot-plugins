from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_DIR.parent
for path in (str(PLUGINS_ROOT), str(Path(__file__).resolve().parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from qqbot_memory_commands import authorization  # noqa: E402
from _support import make_context  # noqa: E402


class TestScopeResolution(unittest.TestCase):
    def test_all_five_canonical_scopes_resolve_with_namespace(self):
        context = make_context(
            "memory_read",
            private=False,
            group_id="group-1",
            allow_global_scope=True,
            actor="actor-1",
            conversation="conversation-1",
            persona="persona-1",
        )
        cases = {
            "global": "all",
            "persona": "persona-1",
            "conversation": "conversation-1",
            "group": "group-1",
            "user": "actor-1",
        }
        for kind, target in cases.items():
            with self.subTest(kind=kind):
                scope = authorization.resolve_scope(context, kind)
                self.assertEqual(scope.kind, kind)
                self.assertEqual(scope.target_id, target)
                self.assertEqual(scope.instance_id, "inst")
                self.assertEqual(scope.platform, "aiocqhttp")
                self.assertEqual(scope.bot_id, "bot-1")
                self.assertIn(f":{kind}:{target}", scope.canonical_id)

    def test_scope_namespace_cannot_be_forged(self):
        context = make_context("memory_read")
        with self.assertRaises(authorization.ScopeResolutionError) as raised:
            authorization.resolve_scope(
                context,
                {
                    "kind": "user",
                    "target_id": "actor-1",
                    "instance_id": "other-instance",
                },
            )
        self.assertEqual(raised.exception.code, "scope_namespace_mismatch")
        with self.assertRaises(authorization.ScopeResolutionError) as raised:
            authorization.resolve_scope(
                context,
                {
                    "kind": "user",
                    "target_id": "actor-1",
                    "platform": "other-platform",
                },
            )
        self.assertEqual(raised.exception.code, "scope_namespace_mismatch")

    def test_conflicting_target_arguments_are_rejected(self):
        context = make_context("memory_read")
        with self.assertRaises(authorization.ScopeResolutionError) as raised:
            authorization.resolve_scope(
                context,
                {"kind": "group", "target_id": "group-1"},
                "group-2",
            )
        self.assertEqual(raised.exception.code, "conflicting_scope_targets")

    def test_global_target_is_fixed_to_all(self):
        context = make_context("memory_read", allow_global_scope=True)
        with self.assertRaises(authorization.ScopeResolutionError):
            authorization.resolve_scope(context, "global", "attacker-global")
        scope = authorization.resolve_scope(context, "global")
        self.assertEqual(scope.target_id, "all")


class TestScopeAuthorization(unittest.TestCase):
    def test_user_scope_is_self_only_even_for_admin(self):
        context = make_context("memory_read", admin=True, roles=("admin",))
        own = authorization.resolve_scope(context, "user", "actor-1")
        self.assertTrue(authorization.authorize_scope(context, own, write=True).allowed)
        other = authorization.resolve_scope(context, "user", "actor-2")
        decision = authorization.authorize_scope(context, other, write=True)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.error_code, "cross_scope_denied")

    def test_group_scope_requires_group_chat_and_membership(self):
        private_context = make_context("memory_read", private=True)
        forged_group = authorization.ScopeRef(
            "group", "group-1", "inst", "aiocqhttp", "bot-1"
        )
        decision = authorization.authorize_scope(
            private_context, forged_group, write=True
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.error_code, "private_group_forgery")

        group_context = make_context(
            "memory_read",
            private=False,
            group_id="group-1",
            allowed_group_ids=("group-2",),
        )
        current = authorization.resolve_scope(group_context, "group", "group-1")
        allowed_cross = authorization.resolve_scope(group_context, "group", "group-2")
        denied_cross = authorization.resolve_scope(group_context, "group", "group-3")
        self.assertTrue(authorization.authorize_scope(group_context, current).allowed)
        self.assertTrue(authorization.authorize_scope(group_context, allowed_cross).allowed)
        self.assertFalse(authorization.authorize_scope(group_context, denied_cross).allowed)

    def test_private_message_cannot_forge_group_target(self):
        context = make_context(
            "memory_read",
            private=True,
            allowed_group_ids=("group-2",),
        )
        group = authorization.ScopeRef(
            "group", "group-2", "inst", "aiocqhttp", "bot-1"
        )
        decision = authorization.authorize_scope(context, group)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.error_code, "private_group_forgery")

    def test_conversation_scope_requires_current_or_explicit_allowlist(self):
        context = make_context(
            "session_read",
            conversation="conversation-1",
            allowed_conversation_ids=("conversation-2",),
        )
        current = authorization.resolve_scope(
            context, "conversation", "conversation-1"
        )
        allowed = authorization.resolve_scope(
            context, "conversation", "conversation-2"
        )
        denied = authorization.resolve_scope(
            context, "conversation", "conversation-3"
        )
        self.assertTrue(authorization.authorize_scope(context, current).allowed)
        self.assertTrue(authorization.authorize_scope(context, allowed).allowed)
        self.assertFalse(authorization.authorize_scope(context, denied).allowed)

    def test_persona_shared_access_requires_flag_and_explicit_id(self):
        context = make_context(
            "persona_switch",
            persona="persona-1",
            allow_shared_persona=True,
            allowed_persona_ids=("persona-2",),
        )
        current = authorization.resolve_scope(context, "persona", "persona-1")
        shared = authorization.resolve_scope(context, "persona", "persona-2")
        not_listed = authorization.resolve_scope(context, "persona", "persona-3")
        self.assertTrue(authorization.authorize_scope(context, current, write=True).allowed)
        decision = authorization.authorize_scope(context, shared, write=True)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.shared)
        self.assertTrue(decision.requires_confirmation)
        self.assertFalse(
            authorization.authorize_scope(context, not_listed, write=True).allowed
        )

    def test_persona_shared_without_flag_is_denied(self):
        context = make_context(
            "persona_switch",
            persona="persona-1",
            allowed_persona_ids=("persona-2",),
        )
        shared = authorization.resolve_scope(context, "persona", "persona-2")
        self.assertFalse(
            authorization.authorize_scope(context, shared, write=True).allowed
        )

    def test_admin_shared_edit_still_requires_explicit_persona_list(self):
        context = make_context(
            "persona_switch",
            persona="persona-1",
            admin=True,
        )
        shared = authorization.resolve_scope(context, "persona", "persona-2")
        self.assertFalse(
            authorization.authorize_scope(
                context,
                shared,
                write=True,
                admin_shared_edit=True,
            ).allowed
        )
        listed = make_context(
            "persona_switch",
            persona="persona-1",
            admin=True,
            allowed_persona_ids=("persona-2",),
        )
        self.assertTrue(
            authorization.authorize_scope(
                listed,
                shared,
                write=True,
                admin_shared_edit=True,
            ).allowed
        )

    def test_global_read_and_write_require_policy_grant(self):
        denied_context = make_context("memory_read")
        scope = authorization.resolve_scope(denied_context, "global")
        self.assertFalse(authorization.authorize_scope(denied_context, scope).allowed)
        allowed_context = make_context("memory_add", allow_global_scope=True)
        scope = authorization.resolve_scope(allowed_context, "global")
        decision = authorization.authorize_scope(allowed_context, scope, write=True)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.shared)
        self.assertTrue(decision.requires_confirmation)

    def test_disallowed_scope_kind_is_denied(self):
        context = make_context("memory_read", private=True)
        scope = authorization.resolve_scope(context, "user", "actor-1")
        decision = authorization.authorize_scope(
            context,
            scope,
            allowed_scopes=("group",),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.error_code, "scope_not_allowed")

    def test_confirmation_policy_for_clear_reset_and_shared_persona(self):
        context = make_context(
            "persona_switch",
            persona="persona-1",
            allow_shared_persona=True,
            allowed_persona_ids=("persona-2",),
            private=False,
            group_id="group-1",
        )
        shared = authorization.resolve_scope(context, "persona", "persona-2")
        user = authorization.resolve_scope(context, "user", "actor-1")
        self.assertTrue(
            authorization.requires_confirmation(
                context,
                shared,
                operation="persona_switch",
            )
        )
        self.assertTrue(
            authorization.requires_confirmation(
                context,
                user,
                operation="memory_confirm_clear",
            )
        )
        self.assertTrue(
            authorization.requires_confirmation(
                context,
                user,
                operation="session_reset",
            )
        )
        self.assertFalse(
            authorization.requires_confirmation(
                context,
                user,
                operation="memory_add",
            )
        )

    def test_canonical_ids_differ_by_instance_platform_and_bot(self):
        ids = set()
        for instance, platform, bot in (
            ("i1", "p", "b"),
            ("i2", "p", "b"),
            ("i1", "q", "b"),
            ("i1", "p", "c"),
        ):
            scope = authorization.ScopeRef(
                "user", "actor", instance, platform, bot
            )
            ids.add(scope.canonical_id)
        self.assertEqual(len(ids), 4)


if __name__ == "__main__":
    unittest.main()

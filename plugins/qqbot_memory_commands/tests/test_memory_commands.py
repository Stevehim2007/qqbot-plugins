"""Tests for the standalone /记忆 command engine (qqbot_memory_commands 4.0.0).

No real AstrBot is required: a small fake memory core/service exercises the
pure command module, exactly like AstrBot's patched ``astrbot.core.memory``
would.  Coverage: command parsing, permission gates, scope isolation, the
unpatched-AstrBot fallback, secret redaction, output bounds and the "never
claim success when the service did not confirm it" rule.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]
GROUP_SCOPE_ID = "aiocqhttp:group-1"
OTHER_GROUP_SCOPE_ID = "aiocqhttp:group-2"
USER_SCOPE_ID = "aiocqhttp:10001"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "qqbot_memory_commands_engine_under_test",
        PLUGIN_DIR / "memory_commands.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MC = _load_module()


# ---------------------------------------------------------------------------
# Fakes (duck-typed AstrBot event / MemoryContext / MemoryService)
# ---------------------------------------------------------------------------
class FakeEvent:
    def __init__(
        self,
        message_str="/记忆 状态",
        *,
        private=False,
        group_id="group-1",
        sender_id="10001",
        role=None,
        sender_role=None,
    ):
        self.message_str = message_str
        self.private = private
        self.group_id = "" if private else group_id
        self.sender_id = sender_id
        self.role = role
        self.message_obj = SimpleNamespace(
            sender=SimpleNamespace(role=sender_role),
        )
        self.unified_msg_origin = (
            f"aiocqhttp:{'FriendMessage' if private else 'GroupMessage'}:session-1"
        )

    def get_sender_id(self):
        return self.sender_id

    def is_private_chat(self):
        return self.private


class CallableAdminEvent(FakeEvent):
    def is_admin(self):  # noqa: D401 - AstrBot v4 style permission helper
        return True


class FakeMemoryContext:
    def __init__(self, *, private=False, group_id="group-1", user_id="10001"):
        self.platform = "aiocqhttp"
        self.user_id = user_id
        self.group_id = "" if private else group_id
        self.is_private = private

    @property
    def user_scope(self):
        return ("user", f"aiocqhttp:{self.user_id}")

    @property
    def group_scope(self):
        if self.is_private or not self.group_id:
            return None
        return ("group", f"aiocqhttp:{self.group_id}")


class FakeService:
    def __init__(self, records=None, *, enabled=True, delete_result=True, forget_result=1):
        self.records = [dict(record) for record in (records or [])]
        self.enabled = enabled
        self.fts_enabled = True
        self.delete_result = delete_result
        self.forget_result = forget_result
        self.export_calls = []
        self.deleted = []
        self.forgotten = []
        self.fail_export = False
        self.fail_delete = False
        self.fail_forget = False

    def export(self, scope_type, scope_id):
        self.export_calls.append((scope_type, scope_id))
        if self.fail_export:
            raise RuntimeError("memory db unavailable")
        return [
            dict(record)
            for record in self.records
            if record.get("scope_type") == scope_type
            and record.get("scope_id") == scope_id
        ]

    def delete(self, memory_id):
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.deleted.append(memory_id)
        if self.delete_result:
            self.records = [
                record for record in self.records if record.get("id") != memory_id
            ]
        return self.delete_result

    def forget(self, scope_type, scope_id):
        if self.fail_forget:
            raise RuntimeError("forget failed")
        self.forgotten.append((scope_type, scope_id))
        matched = [
            record
            for record in self.records
            if record.get("scope_type") == scope_type
            and record.get("scope_id") == scope_id
        ]
        self.records = [
            record
            for record in self.records
            if not (
                record.get("scope_type") == scope_type
                and record.get("scope_id") == scope_id
            )
        ]
        return min(int(self.forget_result), len(matched))

    def policy_for(self, memory_context):
        return SimpleNamespace(
            mode="long_term",
            scope="group",
            inject=True,
            extract=True,
        )


class FakeCore:
    def __init__(self, service=None, memory_context=None):
        self.service = service if service is not None else FakeService()
        self.memory_context = memory_context or FakeMemoryContext()

    def get_memory_service(self, config):
        return self.service

    def build_context(self, event):
        return self.memory_context


class FakeContext:
    def __init__(self, config=None):
        self.config = config if config is not None else {"memory": {"enable": True}}

    def get_config(self, umo=None):
        return self.config


def record(memory_id, content="火锅好吃", *, scope_type="group", scope_id=GROUP_SCOPE_ID, **extra):
    payload = {
        "id": memory_id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "key": f"k{memory_id}",
        "content": content,
        "kind": "fact",
        "importance": 0.6,
        "confidence": 0.8,
        "hits": 0,
        "source": "heuristic",
        "created_at": 1000.0,
        "updated_at": 1000.0,
        "last_used_at": None,
        "expires_at": None,
        "score": 0.0,
    }
    payload.update(extra)
    return payload


def options(**overrides):
    base = dict(
        enabled=True,
        destructive_enabled=False,
        allow_user_ids=(),
        max_results=5,
        content_chars=80,
        max_reply_chars=900,
    )
    base.update(overrides)
    return MC.Options(**base)


def run(
    text="/记忆 状态",
    *,
    event=None,
    service=None,
    memory_context=None,
    opts=None,
    core="auto",
    clock=None,
):
    event = event or FakeEvent(text)
    memory_context = memory_context or FakeMemoryContext(
        private=event.private,
        group_id=event.group_id or "group-1",
        user_id=event.sender_id,
    )
    if core == "auto":
        core = FakeCore(service=service or FakeService(), memory_context=memory_context)
    return MC.handle_text(
        text=text,
        prefixes=("/",),
        event=event,
        context=FakeContext(),
        options=opts or options(),
        core=core,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class TestCommandParsing(unittest.TestCase):
    def test_known_verbs_and_arguments(self):
        cases = {
            "/记忆 状态": ("status", ""),
            "记忆 状态": ("status", ""),
            "/记忆 搜索 火锅 南关区": ("search", "火锅 南关区"),
            "/记忆 删除 12": ("delete", "12"),
            "/记忆 忘记 本群": ("forget", "本群"),
            "/记忆 整理": ("tidy", ""),
            "/记忆": ("help", ""),
            "/记忆 帮助": ("help", ""),
            "/记忆 status": ("status", ""),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = MC.parse_command(text, ("/",))
                self.assertEqual((parsed.action, parsed.argument), expected)

    def test_unknown_verbs_and_non_commands(self):
        self.assertEqual(MC.parse_command("/记忆 乱写", ("/",)).action, "unknown")
        self.assertEqual(MC.parse_command("/记忆搜索", ("/",)).action, "unknown")
        self.assertEqual(MC.parse_command("/别的东西", ("/",)).action, "unknown")
        self.assertEqual(MC.parse_command("", ("/",)).action, "empty")

    def test_custom_prefix_is_stripped(self):
        parsed = MC.parse_command("!记忆 搜索 面", ("!",))
        self.assertEqual((parsed.action, parsed.argument), ("search", "面"))


# ---------------------------------------------------------------------------
# Permission gates
# ---------------------------------------------------------------------------
class TestPermissions(unittest.TestCase):
    def test_disabled_by_default_even_for_admins(self):
        reply = run(event=FakeEvent(role="admin"), opts=MC.Options())
        self.assertEqual(reply, MC.DISABLED_REPLY)

    def test_plain_member_is_denied_without_touching_memory(self):
        service = FakeService(records=[record(1)])
        reply = run(event=FakeEvent(), service=service)
        self.assertEqual(reply, MC.PERMISSION_REPLY)
        self.assertEqual(service.export_calls, [])

    def test_private_non_admin_is_denied(self):
        reply = run(event=FakeEvent("/记忆 状态", private=True))
        self.assertEqual(reply, MC.PERMISSION_REPLY)

    def test_explicit_allowlist_allows_private_sender(self):
        service = FakeService(records=[record(1, scope_type="user", scope_id=USER_SCOPE_ID)])
        reply = run(
            event=FakeEvent("/记忆 状态", private=True),
            service=service,
            opts=options(allow_user_ids=("10001",)),
        )
        self.assertIn("长期记忆管理", reply)
        self.assertEqual(service.export_calls, [("user", USER_SCOPE_ID)])

    def test_admin_role_and_group_owner_are_allowed(self):
        for event in (
            FakeEvent(role="admin"),
            FakeEvent(role="owner"),
            FakeEvent(sender_role="admin"),
            FakeEvent(sender_role="owner"),
            CallableAdminEvent(),
        ):
            with self.subTest(event=event):
                reply = run(event=event, service=FakeService(records=[record(1)]))
                self.assertIn("长期记忆管理", reply)

    def test_permission_reply_is_bounded_and_leaks_nothing(self):
        reply = run(event=FakeEvent("/记忆 搜索 秘密"), service=FakeService(records=[record(1)]))
        self.assertNotIn("火锅", reply)


# ---------------------------------------------------------------------------
# Unpatched / unavailable fallbacks
# ---------------------------------------------------------------------------
class TestFallbacks(unittest.TestCase):
    def test_unpatched_astrbot_answers_gracefully(self):
        original = MC.load_core_memory
        MC.load_core_memory = lambda: None
        try:
            reply = run(event=FakeEvent(role="admin"), core=None)
        finally:
            MC.load_core_memory = original
        self.assertEqual(reply, MC.UNPATCHED_REPLY)

    def test_service_that_cannot_bind_context_is_unavailable(self):
        class BrokenCore:
            def get_memory_service(self, config):
                return FakeService()

            def build_context(self, event):
                raise RuntimeError("no memory patch")

        reply = run(event=FakeEvent(role="admin"), core=BrokenCore())
        self.assertEqual(reply, MC.UNAVAILABLE_REPLY)

    def test_memory_disabled_is_reported_not_faked(self):
        service = FakeService(records=[record(1)], enabled=False)
        reply = run(event=FakeEvent(role="admin"), service=service)
        self.assertEqual(reply, MC.MEMORY_OFF_REPLY)
        self.assertEqual(service.export_calls, [])

    def test_export_failure_is_a_read_failure(self):
        service = FakeService(records=[record(1)])
        service.fail_export = True
        reply = run(event=FakeEvent(role="admin"), service=service)
        self.assertEqual(reply, MC.READ_FAILED_REPLY)


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------
class TestScopeIsolation(unittest.TestCase):
    def test_status_only_counts_the_current_group_scope(self):
        service = FakeService(
            records=[
                record(1, "本群记忆"),
                record(2, "本群记忆二"),
                record(3, "另一个群", scope_id=OTHER_GROUP_SCOPE_ID),
                record(4, "私聊记忆", scope_type="user", scope_id=USER_SCOPE_ID),
            ]
        )
        reply = run(event=FakeEvent(role="admin"), service=service)
        self.assertIn("2 条记忆", reply)
        self.assertEqual(service.export_calls, [("group", GROUP_SCOPE_ID)])

    def test_search_skips_other_groups_and_private_memories(self):
        service = FakeService(
            records=[
                record(1, "本群火锅推荐"),
                record(2, "别的群火锅", scope_id=OTHER_GROUP_SCOPE_ID),
                record(3, "私聊火锅", scope_type="user", scope_id=USER_SCOPE_ID),
            ]
        )
        reply = run("/记忆 搜索 火锅", event=FakeEvent(role="admin"), service=service)
        self.assertIn("本群火锅推荐", reply)
        self.assertNotIn("别的群", reply)
        self.assertNotIn("私聊火锅", reply)
        self.assertIn("[id:1]", reply)
        self.assertNotIn("[id:2]", reply)

    def test_private_search_only_reads_own_user_scope(self):
        service = FakeService(
            records=[
                record(1, "我的火锅", scope_type="user", scope_id=USER_SCOPE_ID),
                record(2, "本群火锅"),
            ]
        )
        event = FakeEvent("/记忆 搜索 火锅", private=True, role="admin")
        reply = run(
            "/记忆 搜索 火锅",
            event=event,
            service=service,
            memory_context=FakeMemoryContext(private=True),
        )
        self.assertIn("我的火锅", reply)
        self.assertNotIn("本群火锅", reply)
        self.assertEqual(service.export_calls, [("user", USER_SCOPE_ID)])

    def test_delete_of_other_scope_id_is_refused_without_calling_delete(self):
        service = FakeService(
            records=[record(2, "别的群", scope_id=OTHER_GROUP_SCOPE_ID)]
        )
        reply = run(
            "/记忆 删除 2",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("未找到 id=2", reply)
        self.assertEqual(service.deleted, [])

    def test_delete_confirms_current_scope_then_deletes(self):
        service = FakeService(records=[record(7, "本群记忆")])
        reply = run(
            "/记忆 删除 7",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("已删除记忆 id=7", reply)
        self.assertEqual(service.deleted, [7])

    def test_delete_returning_false_is_not_reported_as_success(self):
        service = FakeService(records=[record(7)], delete_result=False)
        reply = run(
            "/记忆 删除 7",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("删除失败", reply)
        self.assertNotIn("已删除", reply)

    def test_delete_exception_is_not_reported_as_success(self):
        service = FakeService(records=[record(7)])
        service.fail_delete = True
        reply = run(
            "/记忆 删除 7",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("失败", reply)
        self.assertNotIn("已删除", reply)

    def test_delete_arbitrary_truthy_value_is_not_a_confirmation(self):
        service = FakeService(records=[record(7)], delete_result="ok")
        reply = run(
            "/记忆 删除 7",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("删除失败", reply)
        self.assertNotIn("已删除", reply)

    def test_delete_positive_rowcount_is_confirmation(self):
        service = FakeService(records=[record(7)], delete_result=1)
        reply = run(
            "/记忆 删除 7",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("已删除记忆 id=7", reply)

    def test_forget_requires_an_explicit_scope(self):
        service = FakeService(records=[record(1)])
        reply = run(
            "/记忆 忘记",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("显式指定作用域", reply)
        self.assertEqual(service.forgotten, [])

    def test_forget_group_scope_only_in_groups(self):
        service = FakeService(records=[record(1)])
        reply = run(
            "/记忆 忘记 本群",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("已忘记本群", reply)
        self.assertEqual(service.forgotten, [("group", GROUP_SCOPE_ID)])

    def test_forget_group_scope_is_refused_in_private_chat(self):
        service = FakeService(records=[record(1, scope_type="user", scope_id=USER_SCOPE_ID)])
        reply = run(
            "/记忆 忘记 本群",
            event=FakeEvent(role="admin", private=True),
            service=service,
            opts=options(destructive_enabled=True),
            memory_context=FakeMemoryContext(private=True),
        )
        self.assertIn("私聊没有群记忆", reply)
        self.assertEqual(service.forgotten, [])

    def test_forget_personal_scope_targets_sender_user_scope(self):
        service = FakeService(
            records=[record(1, "我的", scope_type="user", scope_id=USER_SCOPE_ID)]
        )
        reply = run(
            "/记忆 忘记 我的",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("已忘记我的", reply)
        self.assertEqual(service.forgotten, [("user", USER_SCOPE_ID)])

    def test_forget_zero_is_not_reported_as_success(self):
        service = FakeService(records=[], forget_result=1)
        reply = run(
            "/记忆 忘记 本群",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("没有可忘记", reply)
        self.assertNotIn("已忘记", reply)

    def test_forget_exception_is_not_reported_as_success(self):
        service = FakeService(records=[record(1)])
        service.fail_forget = True
        reply = run(
            "/记忆 忘记 本群",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("失败", reply)
        self.assertNotIn("已忘记", reply)

    def test_forget_boolean_confirmation_does_not_invent_a_count(self):
        class RawForgetService(FakeService):
            def forget(self, scope_type, scope_id):
                self.forgotten.append((scope_type, scope_id))
                return True

        service = RawForgetService(records=[record(1)])
        reply = run(
            "/记忆 忘记 本群",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("已忘记本群", reply)
        self.assertIn("未返回条数", reply)
        self.assertNotIn("1 条", reply)

    def test_forget_arbitrary_truthy_value_is_not_confirmation(self):
        class RawForgetService(FakeService):
            def forget(self, scope_type, scope_id):
                self.forgotten.append((scope_type, scope_id))
                return "ok"

        service = RawForgetService(records=[record(1)])
        reply = run(
            "/记忆 忘记 本群",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertNotIn("已忘记", reply)
        self.assertIn("没有可忘记", reply)

    def test_destructive_verbs_need_the_second_switch(self):
        service = FakeService(records=[record(1)])
        for text in ("/记忆 删除 1", "/记忆 忘记 本群"):
            with self.subTest(text=text):
                reply = run(text, event=FakeEvent(role="admin"), service=service)
                self.assertEqual(reply, MC.DESTRUCTIVE_DISABLED_REPLY)
        self.assertEqual(service.deleted, [])
        self.assertEqual(service.forgotten, [])


# ---------------------------------------------------------------------------
# Redaction, truncation, output bounds
# ---------------------------------------------------------------------------
class TestRedactionAndBounds(unittest.TestCase):
    def test_secrets_are_redacted(self):
        service = FakeService(
            records=[
                record(1, "我的密码是 hunter2 后面别提"),
                record(2, "api_key=sk-abcdef123456789 是测试用的"),
                record(3, "Authorization: Bearer abcdef123456"),
            ]
        )
        reply = run("/记忆 搜索 测试", event=FakeEvent(role="admin"), service=service)
        reply += run("/记忆 搜索 密码", event=FakeEvent(role="admin"), service=service)
        reply += run("/记忆 搜索 api_key", event=FakeEvent(role="admin"), service=service)
        for secret in ("hunter2", "sk-abcdef123456789", "Bearer abcdef123456"):
            self.assertNotIn(secret, reply)
        self.assertIn(MC.REDACTED, reply)

    def test_search_respects_max_results(self):
        service = FakeService([record(index, f"火锅{index}") for index in range(1, 21)])
        reply = run(
            "/记忆 搜索 火锅",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(max_results=3),
        )
        self.assertEqual(reply.count("[id:"), 3)
        self.assertIn("其余 17 条未显示", reply)

    def test_reply_length_is_bounded(self):
        service = FakeService([record(index, "火锅" * 100) for index in range(1, 21)])
        reply = run(
            "/记忆 搜索 火锅",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(max_results=10, max_reply_chars=200),
        )
        self.assertLessEqual(len(reply), 200)
        self.assertTrue(reply.endswith("…"))

    def test_single_record_content_is_truncated(self):
        service = FakeService([record(1, "很长" * 300)])
        reply = run(
            "/记忆 搜索 很长",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(content_chars=20),
        )
        line = next(line for line in reply.splitlines() if "[id:1]" in line)
        self.assertLessEqual(len(line), 40)
        self.assertIn("…", line)


# ---------------------------------------------------------------------------
# Tidy (dry-run preview)
# ---------------------------------------------------------------------------
class TestTidyPreview(unittest.TestCase):
    def test_tidy_is_a_dry_run_preview(self):
        service = FakeService(
            records=[
                record(1, "我不吃香菜"),
                record(2, "我不吃香菜"),
                record(3, "我不吃香莱"),
                record(4, "低价值", importance=0.1),
            ]
        )
        reply = run("/记忆 整理", event=FakeEvent(role="admin"), service=service)
        self.assertIn("dry-run 预览", reply)
        self.assertIn("完全重复：1 组", reply)
        self.assertIn("低价值/低置信：1 条", reply)
        self.assertIn("本命令不写入", reply)
        self.assertEqual(service.deleted, [])
        self.assertEqual(service.forgotten, [])
        self.assertEqual(service.export_calls, [("group", GROUP_SCOPE_ID)])

    def test_tidy_detects_expired_records(self):
        service = FakeService(
            records=[record(1, "过期", expires_at=100.0), record(2, "还在", expires_at=None)]
        )
        reply = run(
            "/记忆 整理",
            event=FakeEvent(role="admin"),
            service=service,
            clock=lambda: 200.0,
        )
        self.assertIn("已过期：1 条", reply)

    def test_tidy_empty_scope(self):
        reply = run("/记忆 整理", event=FakeEvent(role="admin"), service=FakeService())
        self.assertIn("还没有记忆", reply)


# ---------------------------------------------------------------------------
# Config helper / plugin metadata contract
# ---------------------------------------------------------------------------
class TestConfigContract(unittest.TestCase):
    def test_schema_and_defaults_ship_the_commands_disabled(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        expected = {
            "enabled": False,
            "destructive_enabled": False,
            "allow_user_ids": "",
            "max_results": 5,
            "content_chars": 80,
            "max_reply_chars": 900,
        }
        self.assertTrue(set(expected).issubset(schema))
        for key, default in expected.items():
            with self.subTest(key=key):
                self.assertIn(key, schema)
                self.assertEqual(schema[key]["default"], default)
                self.assertIn("description", schema[key])
                self.assertIn("type", schema[key])

    def test_schema_enforces_safe_bounds(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["max_results"]["slider"]["min"], 1)
        self.assertEqual(schema["max_results"]["slider"]["max"], 10)
        self.assertEqual(schema["content_chars"]["slider"]["min"], 20)
        self.assertEqual(schema["content_chars"]["slider"]["max"], 200)
        self.assertEqual(schema["max_reply_chars"]["slider"]["min"], 200)
        self.assertEqual(schema["max_reply_chars"]["slider"]["max"], 2000)
        self.assertEqual(schema["enabled"]["type"], "bool")
        self.assertEqual(schema["destructive_enabled"]["type"], "bool")
        self.assertEqual(schema["allow_user_ids"]["type"], "string")
        for key in ("max_results", "content_chars", "max_reply_chars"):
            self.assertEqual(schema[key]["type"], "int")

    def test_plugin_version_and_metadata_match(self):
        version = (PLUGIN_DIR / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "4.1.0")
        metadata = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
        metadata_version = next(
            line.split(":", 1)[1].strip()
            for line in metadata.splitlines()
            if line.startswith("version:")
        )
        self.assertEqual(metadata_version, "4.1.0")
        self.assertIn('astrbot_version: ">=4.29,<5"', metadata)
        self.assertIn("qqbot_memory_commands", metadata)
        init_source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('__version__ = "4.1.0"', init_source)

    def test_readme_documents_required_operational_contracts(self):
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "/记忆管理",
            "group",
            "user",
            "allow_user_ids",
            "destructive_enabled",
            "长期记忆补丁",
            "备份",
            "回滚",
            "不迁移、移动或删除记忆数据",
            "export_all()",
            "should_call_llm(True)",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)

    def test_no_forbidden_plugin_imports(self):
        for path in (
            PLUGIN_DIR / "main.py",
            PLUGIN_DIR / "memory_commands.py",
            PLUGIN_DIR / "__init__.py",
        ):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("qqbot_enhancer", source)
                self.assertNotIn("core_full", source)
                self.assertNotIn("from astrbot.core.llm", source)
                self.assertNotIn("import astrbot.core.llm", source)


class TestOptionsFromConfig(unittest.TestCase):
    def test_empty_and_non_mapping_use_safe_defaults(self):
        for value in (None, {}, [], "not-a-mapping"):
            with self.subTest(value=value):
                opts = MC.options_from_config(value)
                self.assertFalse(opts.enabled)
                self.assertFalse(opts.destructive_enabled)
                self.assertEqual(opts.allow_user_ids, ())
                self.assertEqual(opts.max_results, 5)
                self.assertEqual(opts.content_chars, 80)
                self.assertEqual(opts.max_reply_chars, 900)

    def test_legacy_prefixed_keys_are_ignored(self):
        opts = MC.options_from_config(
            {
                "memory_commands_enabled": True,
                "memory_commands_destructive_enabled": True,
                "memory_commands_allow_user_ids": "10001, 10002",
                "memory_commands_max_results": 8,
                "memory_commands_content_chars": 120,
                "memory_commands_max_reply_chars": 1400,
            }
        )
        self.assertFalse(opts.enabled)
        self.assertFalse(opts.destructive_enabled)
        self.assertEqual(opts.allow_user_ids, ())
        self.assertEqual(opts.max_results, 5)
        self.assertEqual(opts.content_chars, 80)
        self.assertEqual(opts.max_reply_chars, 900)

    def test_canonical_keys_are_read(self):
        opts = MC.options_from_config(
            {
                "enabled": False,
                "destructive_enabled": False,
                "allow_user_ids": "canonical",
                "max_results": 2,
            }
        )
        self.assertFalse(opts.enabled)
        self.assertFalse(opts.destructive_enabled)
        self.assertEqual(opts.allow_user_ids, ("canonical",))
        self.assertEqual(opts.max_results, 2)

    def test_boolean_strings_are_parsed_without_truthy_string_bug(self):
        false_values = ("false", "0", "no", "off", "禁用", "")
        true_values = ("true", "1", "yes", "on", "启用")
        for value in false_values:
            with self.subTest(value=value):
                opts = MC.options_from_config({"enabled": value})
                self.assertFalse(opts.enabled)
        for value in true_values:
            with self.subTest(value=value):
                opts = MC.options_from_config({"enabled": value})
                self.assertTrue(opts.enabled)

    def test_allowlist_accepts_strings_lists_and_deduplicates(self):
        for value, expected in (
            (" 1, 2 ,,1", ("1", "2")),
            (["1", 2, " 1 ", None], ("1", "2")),
            (("a", "b"), ("a", "b")),
        ):
            with self.subTest(value=value):
                opts = MC.options_from_config({"allow_user_ids": value})
                self.assertEqual(opts.allow_user_ids, expected)

    def test_integer_options_are_clamped_and_invalid_values_default(self):
        opts = MC.options_from_config(
            {
                "max_results": 500,
                "content_chars": 1,
                "max_reply_chars": 999999,
            }
        )
        self.assertEqual(opts.max_results, 10)
        self.assertEqual(opts.content_chars, 20)
        self.assertEqual(opts.max_reply_chars, 2000)
        invalid = MC.options_from_config(
            {
                "max_results": "bad",
                "content_chars": None,
                "max_reply_chars": True,
            }
        )
        self.assertEqual(invalid.max_results, 5)
        self.assertEqual(invalid.content_chars, 80)
        self.assertEqual(invalid.max_reply_chars, 900)

    def test_helper_is_pure_and_does_not_mutate_input(self):
        source = {
            "enabled": True,
            "max_results": 3,
        }
        before = dict(source)
        MC.options_from_config(source)
        self.assertEqual(source, before)


class TestAliasesAndAdditionalGuards(unittest.TestCase):
    def test_memory_alias_parses_every_verb(self):
        cases = {
            "/记忆管理 状态": ("status", ""),
            "/记忆管理 搜索 火锅": ("search", "火锅"),
            "/记忆管理 删除 9": ("delete", "9"),
            "/记忆管理 忘记 本群": ("forget", "本群"),
            "/记忆管理 整理": ("tidy", ""),
            "/记忆管理": ("help", ""),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = MC.parse_command(text)
                self.assertEqual((parsed.action, parsed.argument), expected)

    def test_alias_without_word_boundary_is_unknown(self):
        self.assertEqual(
            MC.parse_command("/记忆管理状态").action,
            "unknown",
        )

    def test_search_orders_by_hits_then_updated_at(self):
        service = FakeService(
            [
                record(1, "火锅 old-hit-1", hits=1, updated_at=10.0),
                record(2, "火锅 low", hits=0, updated_at=999.0),
                record(3, "火锅 high-new", hits=9, updated_at=20.0),
                record(4, "火锅 high-old", hits=9, updated_at=10.0),
            ]
        )
        reply = run(
            "/记忆 搜索 火锅",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(max_results=4),
        )
        positions = {
            memory_id: reply.index(f"[id:{memory_id}]") for memory_id in range(1, 5)
        }
        self.assertLess(positions[3], positions[4])
        self.assertLess(positions[4], positions[1])
        self.assertLess(positions[1], positions[2])

    def test_direct_authorization_accepts_comma_separated_allowlist(self):
        self.assertTrue(MC.is_authorized(FakeEvent(), " 10001, 20002 "))

    def test_direct_authorization_tolerates_none_allowlist(self):
        self.assertFalse(MC.is_authorized(FakeEvent(), None))
        self.assertFalse(MC.is_authorized(FakeEvent(), [None, ""]))

    def test_dict_policy_is_reported_in_status(self):
        class DictPolicyService(FakeService):
            def policy_for(self, memory_context):
                return {
                    "mode": "long_term",
                    "scope": "group",
                    "inject": True,
                    "extract": False,
                }

        reply = run(
            "/记忆 状态",
            event=FakeEvent(role="admin"),
            service=DictPolicyService([record(1)]),
        )
        self.assertIn("long_term/group", reply)
        self.assertIn("抽取 关", reply)
        self.assertIn("注入 开", reply)

    def test_export_list_with_non_dict_record_is_read_failure(self):
        class MixedService(FakeService):
            def export(self, scope_type, scope_id):
                self.export_calls.append((scope_type, scope_id))
                return [record(1), "not-a-record"]

        reply = run(
            "/记忆 状态",
            event=FakeEvent(role="admin"),
            service=MixedService(),
        )
        self.assertEqual(reply, MC.READ_FAILED_REPLY)

    def test_private_scope_treats_string_false_as_group(self):
        memory_context = SimpleNamespace(
            is_private="false",
            group_id="group-1",
            user_scope=("user", USER_SCOPE_ID),
            group_scope=("group", GROUP_SCOPE_ID),
        )
        scope = MC.scope_for(memory_context)
        self.assertEqual((scope.kind, scope.scope_id), ("group", GROUP_SCOPE_ID))

    def test_empty_search_does_not_touch_the_service(self):
        service = FakeService([record(1)])
        reply = run("/记忆 搜索   ", event=FakeEvent(role="admin"), service=service)
        self.assertIn("用法", reply)
        self.assertEqual(service.export_calls, [])

    def test_unknown_command_does_not_touch_the_service(self):
        service = FakeService([record(1)])
        reply = run("/记忆 乱写", event=FakeEvent(role="admin"), service=service)
        self.assertIn("未知", reply)
        self.assertEqual(service.export_calls, [])

    def test_foreign_records_returned_by_buggy_export_are_filtered(self):
        class LeakyService(FakeService):
            def export(self, scope_type, scope_id):
                self.export_calls.append((scope_type, scope_id))
                return [
                    record(1, "本群安全"),
                    record(2, "别的群泄漏", scope_id=OTHER_GROUP_SCOPE_ID),
                    record(3, "私聊泄漏", scope_type="user", scope_id=USER_SCOPE_ID),
                ]

        reply = run(
            "/记忆 搜索 泄漏",
            event=FakeEvent(role="admin"),
            service=LeakyService(),
        )
        self.assertNotIn("别的群泄漏", reply)
        self.assertNotIn("私聊泄漏", reply)
        self.assertIn("没有找到", reply)

    def test_export_all_is_never_used(self):
        service = FakeService([record(1, "本群")])
        service.export_all_calls = []

        def forbidden_export_all():
            service.export_all_calls.append(True)
            raise AssertionError("export_all must never be called")

        service.export_all = forbidden_export_all
        run("/记忆 状态", event=FakeEvent(role="admin"), service=service)
        self.assertEqual(service.export_all_calls, [])

    def test_malformed_export_is_a_read_failure(self):
        class BadService(FakeService):
            def export(self, scope_type, scope_id):
                self.export_calls.append((scope_type, scope_id))
                return {"not": "a list"}

        reply = run(
            "/记忆 状态",
            event=FakeEvent(role="admin"),
            service=BadService(),
        )
        self.assertEqual(reply, MC.READ_FAILED_REPLY)

    def test_non_numeric_delete_argument_never_calls_delete(self):
        service = FakeService([record(7)])
        reply = run(
            "/记忆 删除 abc",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("数字", reply)
        self.assertEqual(service.deleted, [])

    def test_private_forget_mine_uses_only_sender_user_scope(self):
        service = FakeService(
            records=[record(1, "我的", scope_type="user", scope_id=USER_SCOPE_ID)]
        )
        reply = run(
            "/记忆 忘记 我的",
            event=FakeEvent(role="admin", private=True),
            service=service,
            opts=options(destructive_enabled=True),
            memory_context=FakeMemoryContext(private=True),
        )
        self.assertIn("已忘记我的", reply)
        self.assertEqual(service.forgotten, [("user", USER_SCOPE_ID)])

    def test_group_forget_mine_uses_sender_user_scope_not_group_scope(self):
        service = FakeService(
            records=[record(1, "我的", scope_type="user", scope_id=USER_SCOPE_ID)]
        )
        reply = run(
            "/记忆 忘记 我的",
            event=FakeEvent(role="admin"),
            service=service,
            opts=options(destructive_enabled=True),
        )
        self.assertIn("已忘记我的", reply)
        self.assertEqual(service.forgotten, [("user", USER_SCOPE_ID)])
        self.assertNotIn(("group", GROUP_SCOPE_ID), service.forgotten)

    def test_service_none_is_unavailable_not_memory_off(self):
        class NoneServiceCore(FakeCore):
            def get_memory_service(self, config):
                return None

        reply = run(event=FakeEvent(role="admin"), core=NoneServiceCore())
        self.assertEqual(reply, MC.UNAVAILABLE_REPLY)
        self.assertNotEqual(reply, MC.MEMORY_OFF_REPLY)

    def test_status_still_works_when_policy_lookup_fails(self):
        class NoPolicyService(FakeService):
            def policy_for(self, memory_context):
                raise RuntimeError("policy unavailable")

        reply = run(
            "/记忆 状态",
            event=FakeEvent(role="admin"),
            service=NoPolicyService([record(1)]),
        )
        self.assertIn("长期记忆管理", reply)
        self.assertIn("1 条记忆", reply)
        self.assertIn("未知/未知", reply)


if __name__ == "__main__":
    unittest.main()

"""The single model gateway and complete-output safe_tools callback."""

from __future__ import annotations

import base64
import inspect
from dataclasses import dataclass
from typing import Any

try:  # package import
    from . import media as media_engine
    from . import persona_bridge as persona_bridge_engine
    from .contracts import (
        ContractError,
        ReplyPlan,
        RequestContext,
        validate_reply_plan,
    )
except ImportError:  # test harness
    import media as media_engine
    import persona_bridge as persona_bridge_engine
    from contracts import (
        ContractError,
        ReplyPlan,
        RequestContext,
        validate_reply_plan,
    )


@dataclass
class ProcessedOutput:
    plan: ReplyPlan
    processed_by_safe_tools: bool
    status: str = "ok"
    error_code: str = ""
    fallback_used: bool = False
    raw_output: str = ""


class ModelGateway:
    """Only this class may call ``Context.llm_generate``."""

    def __init__(self, context: Any, config: Any, state: Any, persona: Any) -> None:
        self.context = context
        self.config = config
        self.state = state
        self.persona = persona

    # ------------------------------------------------------------------
    # Provider selection / generation
    # ------------------------------------------------------------------
    async def await_maybe(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def provider_id_from_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        meta = getattr(value, "meta", None)
        if callable(meta):
            try:
                meta = meta()
            except Exception:
                meta = None
        if meta is not None:
            found = meta.get("id") if isinstance(meta, dict) else getattr(meta, "id", None)
            if found:
                return str(found)
        for attr in ("id", "provider_id"):
            found = getattr(value, attr, None)
            if found:
                return str(found)
        return ""

    async def current_provider_id(self, event: Any) -> str:
        configured = self.config.string("llm.provider_id").strip()
        if configured:
            return configured
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(getter):
            try:
                provider_id = self.provider_id_from_value(
                    await self.await_maybe(getter(umo=umo))
                )
                if provider_id:
                    return provider_id
            except Exception:
                pass
        for name in ("get_using_provider_async", "get_using_provider"):
            getter = getattr(self.context, name, None)
            if not callable(getter):
                continue
            try:
                value = await self.await_maybe(getter(umo=umo))
            except TypeError:
                try:
                    value = await self.await_maybe(getter())
                except Exception:
                    value = None
            except Exception:
                value = None
            provider_id = self.provider_id_from_value(value)
            if provider_id:
                return provider_id
        return ""

    def provider_exists(self, provider_id: str) -> bool:
        getter = getattr(self.context, "get_provider_by_id", None)
        if not callable(getter):
            return True
        try:
            return getter(provider_id) is not None
        except Exception:
            return True

    @staticmethod
    def provider_image_capability(provider: Any) -> bool | None:
        provider_config = getattr(provider, "provider_config", None)
        if not isinstance(provider_config, dict):
            return None
        modalities = provider_config.get("modalities", None)
        if modalities == []:
            return True
        if isinstance(modalities, (list, tuple)) and modalities:
            return "image" in modalities
        return None

    async def llm_generate(self, **kwargs: Any) -> str:
        generator = getattr(self.context, "llm_generate", None)
        if not callable(generator):
            raise RuntimeError("AstrBot Context.llm_generate unavailable")
        timeout = self.config.effective_int(
            "llm.timeout_seconds",
            "llm_timeout_seconds",
            default=45,
            minimum=1,
            maximum=300,
        )
        pending = generator(**kwargs)
        if inspect.isawaitable(pending):
            import asyncio

            response = await asyncio.wait_for(pending, timeout=timeout)
        else:
            response = pending
        if response is None:
            return ""
        if isinstance(response, str):
            return response.strip()
        text = getattr(response, "completion_text", "")
        if text is None:
            return ""
        return str(text).strip()

    async def collect_image_refs(self, event: Any) -> list[str]:
        try:
            from astrbot.api import message_components as Comp
        except Exception:  # pragma: no cover - AstrBot is present in production
            Comp = None
        refs: list[str] = []
        seen: set[str] = set()
        max_images = self.config.int("max_images_per_message", 1, 10)
        try:
            components = list(event.get_messages())
        except Exception:
            components = []
        for component in components:
            image_cls = getattr(Comp, "Image", None) if Comp else None
            if image_cls is None or not isinstance(component, image_cls):
                continue
            if len(refs) >= max_images:
                break
            chosen = ""
            for candidate in (
                getattr(component, "url", None),
                getattr(component, "file", None),
                getattr(component, "path", None),
            ):
                if isinstance(candidate, str) and media_engine.is_media_ref(candidate):
                    chosen = candidate
                    break
            if not chosen:
                converter = getattr(component, "convert_to_base64", None)
                if callable(converter):
                    try:
                        data = await self.await_maybe(converter())
                    except Exception:
                        data = None
                    if isinstance(data, bytes):
                        data = base64.b64encode(data).decode("ascii")
                    if isinstance(data, str) and data.strip():
                        chosen = (
                            data
                            if data.startswith(("base64://", "data:image/"))
                            else f"base64://{data.strip()}"
                        )
            if chosen and chosen not in seen:
                seen.add(chosen)
                refs.append(chosen)
        return refs

    async def generate_reply(
        self,
        event: Any,
        *,
        message_text: str,
        has_face: bool,
        has_image: bool,
        system_prompt: str | None = None,
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        tool_instruction = await self.safe_tools_instruction()
        if system_prompt is None:
            system_prompt = self.persona.effective_system_prompt(
                event, tool_instruction
            )
        elif tool_instruction:
            system_prompt = persona_bridge_engine.inject_safe_tools_instruction(
                system_prompt, tool_instruction
            )
        image_refs = await self.collect_image_refs(event) if has_image else []
        if has_image and not image_refs:
            reply = self.config.string(
                "image_unavailable_reply",
                "我收到图片啦，但暂时无法读取图片内容。",
            )
        elif image_refs:
            reply = await self.reply_to_images(
                event,
                image_refs=image_refs,
                message_text=message_text,
                has_face=has_face,
                system_prompt=system_prompt,
                chat_history=chat_history,
            )
        else:
            reply = await self.reply_to_text(
                event,
                message_text=message_text,
                has_face=has_face,
                system_prompt=system_prompt,
                chat_history=chat_history,
            )
        return self.persona.scrub_identity_from_reply(
            reply, event, chat_history=chat_history
        )

    async def generate_from_request(
        self,
        request_context: RequestContext,
        args: dict[str, Any],
    ) -> str:
        event = args.get("event")
        message_text = str(args.get("message_text", "") or "")
        has_face = bool(args.get("has_face", False))
        has_image = bool(args.get("has_image", False))
        system_prompt = args.get("system_prompt")
        chat_history = args.get("chat_history") or []
        if event is not None:
            return await self.generate_reply(
                event,
                message_text=message_text,
                has_face=has_face,
                has_image=has_image,
                system_prompt=system_prompt,
                chat_history=chat_history,
            )
        provider_id = (
            self.config.string("llm.provider_id").strip()
            or str(args.get("provider_id", "") or "")
        )
        if not provider_id:
            getter = getattr(self.context, "get_using_provider", None)
            if callable(getter):
                try:
                    value = await self.await_maybe(getter())
                    provider_id = self.provider_id_from_value(value)
                except Exception:
                    provider_id = ""
        if not provider_id:
            return self.config.string(
                "llm_error_reply",
                "我暂时没想好怎么回复，稍后再试试吧。",
            )
        prompt = str(
            args.get("prompt")
            or message_text
            or f"请回复会话 {request_context.conversation_id} 的当前消息。"
        )
        system = str(
            system_prompt
            if system_prompt is not None
            else self.config.string("system_prompt")
        )
        try:
            return await self.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system,
                **(
                    {"image_urls": args["image_urls"]}
                    if isinstance(args.get("image_urls"), list) and args["image_urls"]
                    else {}
                ),
            )
        except Exception:
            return self.config.string(
                "llm_error_reply",
                "我暂时没想好怎么回复，稍后再试试吧。",
            )

    async def reply_to_text(
        self,
        event: Any,
        *,
        message_text: str,
        has_face: bool,
        system_prompt: str,
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        provider_id = await self.current_provider_id(event)
        if not provider_id:
            return self.config.string(
                "llm_error_reply", "我暂时没想好怎么回复，稍后再试试吧。"
            )
        prompt = self.persona.build_prompt(
            event,
            message_text,
            has_face=has_face,
            has_image=False,
            chat_history=chat_history,
        )
        try:
            reply = await self.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        except Exception:
            return self.config.string(
                "llm_error_reply", "我暂时没想好怎么回复，稍后再试试吧。"
            )
        return reply or self.config.string("empty_reply_fallback", "嗯嗯，我在听。")

    async def reply_to_images(
        self,
        event: Any,
        *,
        image_refs: list[str],
        message_text: str,
        has_face: bool,
        system_prompt: str,
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        provider_id = await self.current_provider_id(event)
        prompt = self.persona.build_prompt(
            event,
            message_text,
            has_face=has_face,
            has_image=True,
            chat_history=chat_history,
        )
        native_attempted = False
        if provider_id and self.config.bool("native_vision_first", True):
            provider = None
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = getter(provider_id)
                except Exception:
                    provider = None
            capability = self.provider_image_capability(provider)
            if capability is not False:
                native_attempted = True
                try:
                    reply = await self.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        image_urls=image_refs,
                        system_prompt=system_prompt,
                    )
                    if reply:
                        return reply
                except Exception:
                    pass
        caption = await self.caption_images(event, image_refs)
        if caption:
            if provider_id:
                try:
                    caption_prompt = self.persona.build_prompt(
                        event,
                        message_text,
                        has_face=has_face,
                        has_image=True,
                        image_caption=caption,
                        chat_history=chat_history,
                    )
                    reply = await self.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=caption_prompt,
                        system_prompt=system_prompt,
                    )
                    if reply:
                        return reply
                except Exception:
                    pass
            return f"我看了一下图片：{caption}"
        if native_attempted:
            pass
        return self.config.string(
            "image_unavailable_reply",
            "我收到图片啦，但暂时无法读取图片内容。",
        )

    async def caption_images(self, event: Any, image_refs: list[str]) -> str:
        candidates = self.caption_provider_candidates(event)
        if not candidates:
            return ""
        caption_prompt = self.config.string(
            "image_caption_prompt", "请用中文客观描述这张图片的内容。"
        )
        max_caption = self.config.int("max_caption_chars", 1, 5000)
        for provider_id in candidates:
            if not self.provider_exists(provider_id):
                continue
            try:
                caption = await self.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=caption_prompt,
                    image_urls=image_refs,
                    system_prompt="你只负责客观描述图片，不要编造不存在的内容。",
                )
            except Exception:
                continue
            if caption:
                return caption[:max_caption].strip()
        return ""

    def caption_provider_candidates(self, event: Any) -> list[str]:
        candidates: list[str] = []
        plugin_provider = self.config.string("vision_provider_id").strip()
        if plugin_provider:
            candidates.append(plugin_provider)
        if self.config.bool("auto_use_astrbot_caption_provider", True):
            getter = getattr(self.context, "get_config", None)
            if callable(getter):
                try:
                    umo = str(getattr(event, "unified_msg_origin", "") or "")
                    try:
                        config = getter(umo=umo)
                    except TypeError:
                        config = getter()
                except Exception:
                    config = {}
                if isinstance(config, dict):
                    settings = config.get("provider_settings", {})
                    if isinstance(settings, dict):
                        value = str(
                            settings.get("default_image_caption_provider_id", "")
                            or ""
                        ).strip()
                        if value:
                            candidates.append(value)
        return list(dict.fromkeys(candidates))

    # ------------------------------------------------------------------
    # safe_tools handshake
    # ------------------------------------------------------------------
    def safe_tools_star(self) -> Any:
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            meta = getter("qqbot_safe_tools")
        except Exception:
            return None
        if meta is None:
            return None
        if getattr(meta, "activated", True) is False:
            return None
        return getattr(meta, "star_cls", None) or meta

    def safe_tools_available(self) -> bool:
        star = self.safe_tools_star()
        return bool(
            star is not None
            and callable(getattr(star, "tool_instruction", None))
        )

    def process_output_available(self) -> bool:
        star = self.safe_tools_star()
        return bool(
            star is not None
            and callable(getattr(star, "process_model_output", None))
        )

    async def safe_tools_instruction(self) -> str:
        if not self.config.bool("safe_tools_marker_enabled", True):
            return ""
        star = self.safe_tools_star()
        instruction_fn = (
            getattr(star, "tool_instruction", None) if star is not None else None
        )
        if not callable(instruction_fn):
            return ""
        try:
            value = self._call_tool_instruction(instruction_fn)
            value = await self.await_maybe(value)
        except Exception:
            return ""
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _call_tool_instruction(instruction_fn: Any) -> Any:
        try:
            signature = inspect.signature(instruction_fn)
        except (TypeError, ValueError):
            return instruction_fn()
        required = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and parameter.default is inspect.Parameter.empty
        ]
        return instruction_fn(None) if required else instruction_fn()

    # ------------------------------------------------------------------
    # Complete-output safe_tools callback
    # ------------------------------------------------------------------
    def _fallback_plan(
        self,
        request_context: RequestContext,
        text: str,
        delivery_key: str = "",
    ) -> ReplyPlan:
        return ReplyPlan(
            request_context=request_context,
            text_segments=[self.finalize_text(text)],
            terminal=True,
            delivery_key=delivery_key,
        )

    def finalize_text(self, text: Any) -> str:
        value = str(text or "").strip()
        if not value:
            value = self.config.string("empty_reply_fallback", "嗯嗯，我在听。")
        limit = self.config.int("max_reply_chars", 1, 4000)
        if limit > 0 and len(value) > limit:
            value = value[:limit].rstrip() + "…"
        return value

    def _payload_to_plan(
        self,
        payload: Any,
        request_context: RequestContext,
        expected_delivery_key: str,
    ) -> Any:
        if payload is None:
            return None
        if isinstance(payload, ReplyPlan):
            return payload
        if hasattr(payload, "to_dict") and not isinstance(payload, (str, dict)):
            payload = payload.to_dict()
        if isinstance(payload, dict):
            if "reply_plan" in payload:
                return self._payload_to_plan(
                    payload["reply_plan"],
                    request_context,
                    expected_delivery_key,
                )
            if "text_segments" in payload or "delivery_key" in payload:
                normalized = dict(payload)
                normalized.setdefault("request_context", request_context.to_dict())
                normalized.setdefault("delivery_key", expected_delivery_key)
                normalized.setdefault("media_segments", [])
                normalized.setdefault("terminal", True)
                return normalized
            if "data" in payload:
                return self._payload_to_plan(
                    payload["data"],
                    request_context,
                    expected_delivery_key,
                )
        return None

    def _validate_safe_tools_response(
        self,
        result: Any,
        request_context: RequestContext,
        *,
        expected_call_id: str,
        expected_delivery_key: str,
    ) -> ReplyPlan:
        payload = result
        if hasattr(result, "to_dict") and not isinstance(result, (str, dict, ReplyPlan)):
            result = result.to_dict()
        if isinstance(result, dict) and "status" in result and "data" in result:
            status = str(result.get("status", "")).lower()
            if status not in {"ok", "success"}:
                raise ContractError(
                    str(result.get("error_code") or "internal_error"),
                    "safe_tools output processing failed",
                )
            payload = result.get("data")
        plan_payload = self._payload_to_plan(
            payload,
            request_context,
            expected_delivery_key,
        )
        if plan_payload is None:
            if isinstance(payload, str):
                return self._fallback_plan(
                    request_context,
                    payload,
                    expected_delivery_key,
                )
            raise ContractError("invalid_args", "safe_tools returned no ReplyPlan")
        plan = validate_reply_plan(plan_payload)
        try:
            plan_context = plan.request_context
        except Exception:
            plan_context = None
        if plan_context is not None:
            if plan_context.turn_id and plan_context.turn_id != request_context.turn_id:
                raise ContractError("invalid_args", "reply_plan turn_id mismatch")
            if int(plan_context.owner_epoch) != int(self.state.owner_epoch):
                raise ContractError(
                    "owner_epoch_mismatch", "reply_plan owner_epoch mismatch"
                )
        result_call_id = ""
        result_callback = None
        if isinstance(payload, dict):
            result_call_id = str(payload.get("call_id", "") or "")
            result_callback = payload.get("callback")
        if result_call_id and expected_call_id and result_call_id != expected_call_id:
            raise ContractError("invalid_args", "reply_plan call_id mismatch")
        if result_callback is not None and not callable(result_callback):
            raise ContractError("invalid_args", "reply_plan callback is not callable")
        if not plan.delivery_key:
            plan.delivery_key = expected_delivery_key
        elif expected_delivery_key and plan.delivery_key != expected_delivery_key:
            raise ContractError("invalid_args", "reply_plan delivery_key mismatch")
        return plan

    async def process_model_output(
        self,
        request_context: RequestContext,
        full_model_output: str,
        *,
        expected_call_id: str = "",
        expected_delivery_key: str = "",
    ) -> ProcessedOutput:
        output = str(full_model_output or "")
        try:
            if int(request_context.owner_epoch) != int(self.state.owner_epoch):
                raise ContractError("owner_epoch_mismatch", "owner_epoch mismatch")
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError("invalid_args", "owner_epoch invalid") from exc
        star = self.safe_tools_star()
        processor = (
            getattr(star, "process_model_output", None)
            if star is not None
            else None
        )
        if not callable(processor):
            # No safe_tools => no marker protocol is advertised; the complete
            # text passes through unchanged.
            return ProcessedOutput(
                plan=self._fallback_plan(
                    request_context,
                    output,
                    expected_delivery_key,
                ),
                processed_by_safe_tools=False,
                raw_output=output,
            )
        try:
            result = processor(request_context, output)
            result = await self.await_maybe(result)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError("internal_error", "safe_tools processor failed") from exc
        try:
            plan = self._validate_safe_tools_response(
                result,
                request_context,
                expected_call_id=expected_call_id,
                expected_delivery_key=expected_delivery_key,
            )
        except ContractError:
            raise
        return ProcessedOutput(
            plan=plan,
            processed_by_safe_tools=True,
            status="ok",
            raw_output=output,
        )

    @staticmethod
    def plan_text(plan: ReplyPlan) -> str:
        return "\n".join(str(item) for item in plan.text_segments if str(item)).strip()

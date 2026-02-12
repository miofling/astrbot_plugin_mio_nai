import asyncio
import base64
import io
import json
import random
import re
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import Message
from astrbot.core.message.message_event_result import MessageChain


@dataclass
class DrawTask:
    origin: Any
    description: str
    source: str
    user_id: str
    group_id: str
    use_llm: bool
    is_auto: bool = False
    submit_ts: float = field(default_factory=time.time)


@register(
    "astrbot_plugin_mio_nai",
    "miofling",
    "Mio 的 NovelAI 绘图插件（基础/辅助/自动/队列）",
    "0.2.10",
    "https://github.com/miofling/astrbot_plugin_mio_nai",
)
class MioNaiPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}

        self.plugin_dir = Path(__file__).resolve().parent
        self.data_dir = self.plugin_dir / "data"
        self.cache_dir = self.data_dir / "cache"
        self.state_path = self.data_dir / "runtime_state.json"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.state = self._build_runtime_state()

        self.draw_queue: asyncio.Queue[DrawTask] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._auto_task: Optional[asyncio.Task] = None
        self._working = False

        self.group_context_buffers: Dict[str, Deque[str]] = {}
        self.group_last_origin: Dict[str, Any] = {}
        self.group_next_time: Dict[str, float] = {}

        self._ensure_background_tasks()

    def _build_runtime_state(self) -> Dict[str, Any]:
        state = {
            "nai_api_url": "https://ai.mio-ai.cyou/ai/generate-image",
            "nai_api_key": "",
            "nai_model": "nai-diffusion-4-5-full",
            "worker_token": "",
            "http_user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "default_positive_tags": "best quality, very aesthetic, absurdres",
            "append_positive_tags": "best quality, 8k",
            "default_negative_tags": (
                "lowres, bad anatomy, bad hands, text, error, missing fingers, "
                "extra digit, fewer digits, cropped, worst quality, low quality, "
                "normal quality, jpeg artifacts, signature, watermark, username, blurry"
            ),
            "width": 832,
            "height": 1216,
            "steps": 28,
            "scale": 6.0,
            "sampler": "k_euler_ancestral",
            "seed": -1,
            "timeout_sec": 120,
            "retry_429_enabled": True,
            "retry_429_max_attempts": 3,
            "retry_429_base_delay_sec": 3.0,
            "retry_429_max_delay_sec": 20.0,
            "log_request_payload": False,
            "opus_mode": True,
            "echo_meta": True,
            "send_processing_text": True,
            "enable_assist": True,
            "basic_draw_require_tags": True,
            "basic_draw_non_tag_warning": (
                "基础画图只接受标签语言，请使用逗号分隔 tags。"
                "例如：1girl, white hair, red eyes, cat ears。"
                "自然语言请使用 /辅助画图。"
            ),
            "tip_started_template": "{mode}已开始生成。",
            "tip_queued_template": "{mode}已加入队列，前方还有 {pending} 个任务。",
            "bot_names": ["mio", "米欧"],
            "draw_keywords": ["画", "画图", "生图", "绘图", "生成", "来一张", "来张"],
            "whitelist_group_ids": [],
            "admin_user_ids": [],
            "auto_enabled": False,
            "auto_mode": "context",
            "auto_min_minutes": 30,
            "auto_max_minutes": 90,
            "auto_context_window": 20,
            "auto_random_prompts": [],
            "auto_queue_limit": 3,
            "llm_enable": True,
            "llm_provider_id": "",
            "llm_api_url": "",
            "llm_api_key": "",
            "llm_model": "",
            "llm_timeout_sec": 60,
            "llm_system_prompt": (
                "You are a prompt engineer for NovelAI anime image generation. "
                "Convert user request or chat context into concise English booru-style tags. "
                "Output ONLY comma-separated tags, no explanations, no markdown."
            ),
        }

        if self.state_path.exists():
            try:
                disk_state = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(disk_state, dict):
                    state.update(disk_state)
            except Exception as exc:
                logger.warning(f"[mio_nai] runtime_state.json 读取失败，将使用当前配置: {exc}")

        # 配置文件优先级高于运行时状态，避免 UI 改配置后被 runtime_state 覆盖。
        collected_config: Dict[str, Any] = {}
        self._collect_config_values(self.config, set(state.keys()), collected_config)
        for key, value in collected_config.items():
            if value is not None:
                state[key] = value

        for key in (
            "bot_names",
            "draw_keywords",
            "whitelist_group_ids",
            "admin_user_ids",
            "auto_random_prompts",
        ):
            state[key] = self._as_list(state.get(key, []))

        return state

    def _save_runtime_state(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _ensure_background_tasks(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = loop.create_task(self._draw_worker_loop())
        if self._auto_task is None or self._auto_task.done():
            self._auto_task = loop.create_task(self._auto_scheduler_loop())

    async def terminate(self):
        for task in (self._worker_task, self._auto_task):
            if task and not task.done():
                task.cancel()

    @filter.command("画图")
    async def cmd_draw(self, event: AstrMessageEvent, description: str = ""):
        self._ensure_background_tasks()
        desc = self._extract_command_argument_from_event(
            event=event,
            command_name="画图",
            fallback=(description or ""),
            allow_no_space=True,
        )
        if not desc:
            yield event.plain_result("用法: /画图 描述")
            return

        group_id = self._get_group_id(event)
        if group_id and not self._is_group_allowed(group_id):
            yield event.plain_result("当前群不在画图白名单中。")
            return

        if self._to_bool(self.state.get("basic_draw_require_tags", True)):
            if not self._looks_like_tag_prompt(desc):
                warn = str(self.state.get("basic_draw_non_tag_warning", "")).strip()
                if not warn:
                    warn = (
                        "基础画图只接受标签语言，例如："
                        "1girl, white hair, red eyes, cat ears。"
                    )
                yield event.plain_result(warn)
                return

        pending = await self._enqueue_task(
            event=event,
            description=desc,
            source="basic",
            use_llm=False,
            is_auto=False,
        )
        if self._to_bool(self.state.get("send_processing_text", True)):
            yield event.plain_result(self._queue_tip("基础画图", pending))

    @filter.command("辅助画图")
    async def cmd_assist_draw(self, event: AstrMessageEvent, description: str = ""):
        self._ensure_background_tasks()
        desc = self._extract_command_argument_from_event(
            event=event,
            command_name="辅助画图",
            fallback=(description or ""),
            allow_no_space=True,
        )
        if not desc:
            yield event.plain_result("用法: /辅助画图 描述")
            return

        group_id = self._get_group_id(event)
        if group_id and not self._is_group_allowed(group_id):
            yield event.plain_result("当前群不在画图白名单中。")
            return

        pending = await self._enqueue_task(
            event=event,
            description=desc,
            source="assist",
            use_llm=True,
            is_auto=False,
        )
        if self._to_bool(self.state.get("send_processing_text", True)):
            yield event.plain_result(self._queue_tip("辅助画图", pending))

    @filter.command("画图状态")
    async def cmd_status(self, event: AstrMessageEvent):
        queue_size = self.draw_queue.qsize() + (1 if self._working else 0)
        auto_enabled = self._to_bool(self.state.get("auto_enabled", False))
        auto_mode = str(self.state.get("auto_mode", "context"))
        whitelist = self._normalized_groups(self.state.get("whitelist_group_ids", []))
        msg = (
            f"队列任务: {queue_size}\n"
            f"自动画图: {'开启' if auto_enabled else '关闭'} ({auto_mode})\n"
            f"白名单群: {', '.join(whitelist) if whitelist else '未限制'}\n"
            f"模型: {self.state.get('nai_model', '')}"
        )
        yield event.plain_result(msg)

    @filter.command("画图管理")
    async def cmd_manage(self, event: AstrMessageEvent, content: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以修改配置。")
            return

        text = self._extract_command_argument_from_event(
            event=event,
            command_name="画图管理",
            fallback=(content or ""),
            allow_no_space=True,
        )
        text = self._normalize_manage_text(text)
        if not text or text in {"help", "帮助"}:
            yield event.plain_result(self._manage_help_text())
            return

        tokens = text.split()
        cmd = tokens[0].lower()

        if cmd in {"查看", "show"}:
            yield event.plain_result(self._config_snapshot())
            return

        if cmd in {"白名单", "wl"}:
            result = self._manage_whitelist(tokens[1:])
            yield event.plain_result(result)
            return

        if cmd in {"自动", "auto"} and len(tokens) >= 2:
            val = tokens[1].lower()
            if val in {"开", "on", "1", "true"}:
                self.state["auto_enabled"] = True
            elif val in {"关", "off", "0", "false"}:
                self.state["auto_enabled"] = False
            else:
                yield event.plain_result("用法: /画图管理 自动 开|关")
                return
            self._save_runtime_state()
            yield event.plain_result(f"自动画图已{'开启' if self.state['auto_enabled'] else '关闭'}。")
            return

        if cmd in {"模式", "mode"} and len(tokens) >= 2:
            val = tokens[1].lower()
            if val in {"上下文", "context"}:
                self.state["auto_mode"] = "context"
            elif val in {"随机", "random"}:
                self.state["auto_mode"] = "random"
            else:
                yield event.plain_result("用法: /画图管理 模式 上下文|随机")
                return
            self._save_runtime_state()
            yield event.plain_result(f"自动画图模式已切换为: {self.state['auto_mode']}")
            return

        if cmd in {"间隔", "interval"} and len(tokens) >= 3:
            try:
                min_m = int(tokens[1])
                max_m = int(tokens[2])
                if min_m <= 0 or max_m <= 0 or min_m > max_m:
                    raise ValueError
            except ValueError:
                yield event.plain_result("用法: /画图管理 间隔 <最小分钟> <最大分钟>")
                return
            self.state["auto_min_minutes"] = min_m
            self.state["auto_max_minutes"] = max_m
            self._save_runtime_state()
            yield event.plain_result(f"自动画图间隔已更新: {min_m}~{max_m} 分钟")
            return

        if cmd in {"正面", "positive"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 正面 <tags>")
                return
            self.state["default_positive_tags"] = value
            self._save_runtime_state()
            yield event.plain_result("默认正面 tags 已更新。")
            return

        if cmd in {"负面", "negative"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 负面 <tags>")
                return
            self.state["default_negative_tags"] = value
            self._save_runtime_state()
            yield event.plain_result("默认负面 tags 已更新。")
            return

        if cmd in {"追加", "append"}:
            value = text[len(tokens[0]) :].strip()
            self.state["append_positive_tags"] = value
            self._save_runtime_state()
            yield event.plain_result("追加正面 tags 已更新。")
            return

        if cmd in {"回显", "echo"} and len(tokens) >= 2:
            val = tokens[1].lower()
            self.state["echo_meta"] = val in {"开", "on", "1", "true"}
            self._save_runtime_state()
            yield event.plain_result(f"元信息回显已{'开启' if self.state['echo_meta'] else '关闭'}。")
            return

        if cmd in {"opus"} and len(tokens) >= 2:
            val = tokens[1].lower()
            self.state["opus_mode"] = val in {"开", "on", "1", "true"}
            self._save_runtime_state()
            yield event.plain_result(f"Opus 参数模式已{'开启' if self.state['opus_mode'] else '关闭'}。")
            return

        if cmd in {"请求日志开", "reqlogon"}:
            self.state["log_request_payload"] = True
            self._save_runtime_state()
            yield event.plain_result("请求 JSON 日志已开启。")
            return

        if cmd in {"请求日志关", "reqlogoff"}:
            self.state["log_request_payload"] = False
            self._save_runtime_state()
            yield event.plain_result("请求 JSON 日志已关闭。")
            return

        if cmd in {"请求日志", "reqlog"} and len(tokens) >= 2:
            val = tokens[1].lower()
            self.state["log_request_payload"] = val in {"开", "on", "1", "true"}
            self._save_runtime_state()
            yield event.plain_result(
                f"请求 JSON 日志已{'开启' if self.state['log_request_payload'] else '关闭'}。"
            )
            return

        if cmd in {"文案开始", "tipstart"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result(
                    "用法: /画图管理 文案开始 <文案>\n支持占位符: {mode}, {pending}"
                )
                return
            self.state["tip_started_template"] = value
            self._save_runtime_state()
            yield event.plain_result("开始生成文案已更新。")
            return

        if cmd in {"文案排队", "tipqueue"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result(
                    "用法: /画图管理 文案排队 <文案>\n支持占位符: {mode}, {pending}"
                )
                return
            self.state["tip_queued_template"] = value
            self._save_runtime_state()
            yield event.plain_result("排队文案已更新。")
            return

        if cmd in {"基础警告", "tagwarn"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 基础警告 <提示文案>")
                return
            self.state["basic_draw_non_tag_warning"] = value
            self._save_runtime_state()
            yield event.plain_result("基础画图警告文案已更新。")
            return

        if cmd in {"llm开", "llmon"}:
            self.state["llm_enable"] = True
            self._save_runtime_state()
            yield event.plain_result("LLM 优化已开启。")
            return

        if cmd in {"llm关", "llmoff"}:
            self.state["llm_enable"] = False
            self._save_runtime_state()
            yield event.plain_result("LLM 优化已关闭。")
            return

        if cmd in {"llmprovider"}:
            value = text[len(tokens[0]) :].strip()
            self.state["llm_provider_id"] = value
            self._save_runtime_state()
            if value:
                yield event.plain_result(f"LLM Provider 已更新: {value}")
            else:
                yield event.plain_result("LLM Provider 已清空，将自动跟随当前会话模型。")
            return

        if cmd in {"llmurl"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 llmurl <url>")
                return
            self.state["llm_api_url"] = value
            self._save_runtime_state()
            yield event.plain_result("LLM URL 已更新。")
            return

        if cmd in {"llmkey"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 llmkey <key>")
                return
            self.state["llm_api_key"] = value
            self._save_runtime_state()
            yield event.plain_result("LLM Key 已更新。")
            return

        if cmd in {"llmmodel"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 llmmodel <model>")
                return
            self.state["llm_model"] = value
            self._save_runtime_state()
            yield event.plain_result("LLM Model 已更新。")
            return

        if cmd in {"api", "naiurl"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 api <url>")
                return
            self.state["nai_api_url"] = value
            self._save_runtime_state()
            yield event.plain_result("NAI API URL 已更新。")
            return

        if cmd in {"key", "naikey"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 key <nai_api_key>")
                return
            self.state["nai_api_key"] = value
            self._save_runtime_state()
            yield event.plain_result("NAI API Key 已更新。")
            return

        if cmd in {"model"}:
            value = text[len(tokens[0]) :].strip()
            if not value:
                yield event.plain_result("用法: /画图管理 model <nai_model>")
                return
            self.state["nai_model"] = value
            self._save_runtime_state()
            yield event.plain_result("NAI 模型已更新。")
            return

        yield event.plain_result("未知子命令。输入 /画图管理 帮助 查看可用命令。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_messages(self, event: AstrMessageEvent):
        self._ensure_background_tasks()
        raw_text = str(getattr(event, "message_str", "") or "").strip()
        if not raw_text:
            return

        group_id = self._get_group_id(event)
        if group_id:
            self._remember_group_context(group_id, raw_text, getattr(event, "unified_msg_origin", None))

        if self._looks_like_command_message(raw_text):
            return

        if self._should_assist_draw(event, raw_text):
            description = self._extract_natural_description(raw_text)
            if not description:
                yield event.plain_result("我懂你的意思了，但描述太短。可以再具体一点。")
                return

            pending = await self._enqueue_task(
                event=event,
                description=description,
                source="assist_natural",
                use_llm=True,
                is_auto=False,
            )
            if self._to_bool(self.state.get("send_processing_text", True)):
                yield event.plain_result(self._queue_tip("辅助画图", pending))

    def _looks_like_command_message(self, text: str) -> bool:
        cleaned = text.strip()
        # 去掉消息前缀中的 CQ 段（如 reply/at），再判断是否是斜杠命令。
        for _ in range(3):
            new_cleaned = re.sub(r"^\[CQ:[^\]]+\]\s*", "", cleaned, flags=re.IGNORECASE)
            if new_cleaned == cleaned:
                break
            cleaned = new_cleaned
        cleaned = cleaned.lstrip()
        if cleaned.startswith(("/", "／")):
            return True
        # 兜底：消息中出现本插件命令前缀也视为命令，避免误触发辅助画图。
        if re.search(r"[/／](?:画图|辅助画图|画图状态|画图管理)\b", cleaned):
            return True
        return False

    def _extract_command_argument_from_event(
        self,
        event: AstrMessageEvent,
        command_name: str,
        fallback: str = "",
        allow_no_space: bool = False,
    ) -> str:
        fallback_text = str(fallback or "").strip()
        candidates = self._collect_event_text_candidates(event)
        for candidate in candidates:
            parsed = self._extract_command_argument(
                candidate,
                command_name=command_name,
                allow_no_space=allow_no_space,
            )
            if parsed:
                return parsed
        if fallback_text:
            logger.warning(
                "[mio_nai] 命令参数回退到框架参数: command=%s fallback=%s",
                command_name,
                self._trim_text_for_log(fallback_text, 120),
            )
        return fallback_text

    def _collect_event_text_candidates(self, event: AstrMessageEvent) -> List[str]:
        values: List[str] = []

        def add(val: Any) -> None:
            if not isinstance(val, str):
                return
            text = val.strip()
            if text and text not in values:
                values.append(text)

        add(getattr(event, "message_str", None))

        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            for attr in ("message_str", "raw_message", "raw_text", "text", "plain_text"):
                add(getattr(msg_obj, attr, None))

            message = getattr(msg_obj, "message", None)
            if isinstance(message, str):
                add(message)
            elif isinstance(message, list):
                parts: List[str] = []
                for component in message:
                    comp_text = self._component_to_text(component)
                    if comp_text:
                        parts.append(comp_text)
                if parts:
                    add("".join(parts))
                    add(" ".join(parts))

        return values

    def _component_to_text(self, component: Any) -> str:
        if component is None:
            return ""
        if isinstance(component, str):
            return component

        for attr in ("text", "content", "message", "raw_message"):
            val = getattr(component, attr, None)
            if isinstance(val, str) and val.strip():
                return val

        get_plain_text = getattr(component, "get_plain_text", None)
        if callable(get_plain_text):
            try:
                val = get_plain_text()
                if isinstance(val, str) and val.strip():
                    return val
            except Exception:
                pass

        return ""

    def _extract_command_argument(
        self,
        raw_text: str,
        command_name: str,
        allow_no_space: bool = False,
    ) -> str:
        cleaned = str(raw_text or "")
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip()
        if not cleaned:
            return ""

        sep_pattern = r"(?:\s*[:：]\s*|\s+)"
        if allow_no_space:
            sep_pattern = r"(?:\s*[:：]\s*|\s+|\s*)"

        pattern = rf"[／/]\s*{re.escape(command_name)}{sep_pattern}(?P<arg>[\s\S]*)$"
        matches = list(re.finditer(pattern, cleaned, flags=re.IGNORECASE))
        for match in reversed(matches):
            arg = str(match.group("arg") or "").strip()
            if command_name == "画图" and arg.startswith(("管理", "状态")):
                continue
            return arg
        return ""

    def _normalize_manage_text(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip())
        if not normalized:
            return ""
        if " " in normalized:
            return normalized

        toggle_match = re.match(r"^(自动|回显|opus|请求日志)(开|关)$", normalized, flags=re.IGNORECASE)
        if toggle_match:
            return f"{toggle_match.group(1)} {toggle_match.group(2)}"

        mode_match = re.match(r"^(模式)(上下文|随机)$", normalized, flags=re.IGNORECASE)
        if mode_match:
            return f"{mode_match.group(1)} {mode_match.group(2)}"

        kv_prefixes = [
            "正面",
            "负面",
            "追加",
            "文案开始",
            "文案排队",
            "基础警告",
            "llmprovider",
            "llmurl",
            "llmkey",
            "llmmodel",
            "api",
            "key",
            "model",
        ]
        lowered = normalized.lower()
        for prefix in kv_prefixes:
            if lowered.startswith(prefix.lower()) and len(normalized) > len(prefix):
                value = normalized[len(prefix) :].lstrip(" :：")
                if value:
                    return f"{prefix} {value}"

        return normalized

    def _looks_like_tag_prompt(self, text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        normalized = cleaned.replace("，", ",").replace("；", ",")
        if re.search(r"[\u4e00-\u9fff]", normalized):
            return False
        if re.search(r"[。！？!?]", normalized):
            return False

        parts = [x.strip() for x in normalized.split(",") if x.strip()]
        if not parts:
            return False

        token_pattern = r"[A-Za-z0-9_:\-+./()' ]{1,80}"
        if len(parts) == 1:
            token = parts[0]
            if len(token.split()) > 4:
                return False
            return re.fullmatch(token_pattern, token) is not None

        for part in parts:
            if re.fullmatch(token_pattern, part) is None:
                return False
        return True

    async def _enqueue_task(
        self,
        event: AstrMessageEvent,
        description: str,
        source: str,
        use_llm: bool,
        is_auto: bool,
    ) -> int:
        origin = getattr(event, "unified_msg_origin", None)
        user_id = self._get_sender_id(event)
        group_id = self._get_group_id(event)
        pending_before = self.draw_queue.qsize() + (1 if self._working else 0)
        task = DrawTask(
            origin=origin,
            description=description,
            source=source,
            user_id=user_id,
            group_id=group_id,
            use_llm=use_llm,
            is_auto=is_auto,
        )
        await self.draw_queue.put(task)
        return pending_before

    async def _draw_worker_loop(self):
        while True:
            task = await self.draw_queue.get()
            self._working = True
            try:
                await self._process_draw_task(task)
            except Exception as exc:
                logger.exception(f"[mio_nai] 绘图任务处理异常: {exc}")
                await self._send_plain(task.origin, f"绘图失败: {exc}")
            finally:
                self._working = False
                self.draw_queue.task_done()

    async def _process_draw_task(self, task: DrawTask):
        started_at = time.time()
        logger.info(
            "[mio_nai] start task source=%s auto=%s group=%s",
            task.source,
            task.is_auto,
            task.group_id or "private",
        )

        prompt_core = task.description.strip()
        if task.use_llm:
            prompt_core = await self._optimize_prompt_with_llm(
                task.description,
                task.group_id,
                task.origin,
            )
        logger.info("[mio_nai] prompt prepared, sending to NAI")

        positive = self._join_tags(
            [
                str(self.state.get("default_positive_tags", "")),
                str(self.state.get("append_positive_tags", "")),
                prompt_core,
            ]
        )
        negative = str(self.state.get("default_negative_tags", "")).strip()

        payload, seed = self._build_nai_payload(positive, negative)
        image_bytes, ext = await self._call_nai(payload)
        image_path = self._save_image_file(image_bytes, ext)

        elapsed = round(time.time() - started_at, 2)
        echo_meta = self._to_bool(self.state.get("echo_meta", True))
        if echo_meta:
            meta = (
                f"{'自动' if task.is_auto else '完成'} | 模型: {self.state.get('nai_model')} | "
                f"seed: {seed} | 耗时: {elapsed}s\nprompt: {positive}"
            )
            chain = MessageChain().message(str(meta)).file_image(str(image_path))
        else:
            chain = MessageChain().file_image(str(image_path))
        await self._send_chain(task.origin, chain)

    async def _call_nai(self, payload: Dict[str, Any]) -> Tuple[bytes, str]:
        url = str(self.state.get("nai_api_url", "")).strip()
        key = str(self.state.get("nai_api_key", "")).strip()
        if not url:
            raise RuntimeError("未配置 nai_api_url")
        if not key:
            raise RuntimeError("未配置 nai_api_key")

        timeout = int(self.state.get("timeout_sec", 120))
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://novelai.net",
            "Referer": "https://novelai.net",
            "User-Agent": str(self.state.get("http_user_agent", "")).strip()
            or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        }
        worker_token = str(self.state.get("worker_token", "")).strip()
        if worker_token:
            headers["X-Worker-Token"] = worker_token

        if self._to_bool(self.state.get("log_request_payload", False)):
            try:
                payload_for_log = self._payload_for_log(payload)
                logger.info(
                    "[mio_nai] NAI request payload: %s",
                    json.dumps(payload_for_log, ensure_ascii=False),
                )
            except Exception:
                pass

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        retry_429_enabled = self._to_bool(self.state.get("retry_429_enabled", True))
        retry_429_max_attempts = max(0, int(self.state.get("retry_429_max_attempts", 3)))
        retry_429_base_delay_sec = max(
            0.5, float(self.state.get("retry_429_base_delay_sec", 3.0))
        )
        retry_429_max_delay_sec = max(
            retry_429_base_delay_sec,
            float(self.state.get("retry_429_max_delay_sec", 20.0)),
        )

        attempt = 0
        while True:
            try:
                status, resp_headers, body = await asyncio.to_thread(
                    self._http_post_raw, url, headers, data, timeout
                )
                if status >= 400:
                    snippet = body[:220].decode("utf-8", errors="ignore")
                    raise RuntimeError(f"HTTPError {status}: {snippet}")
            except RuntimeError as exc:
                err = str(exc)
                if retry_429_enabled and self._is_http_429_error(err):
                    if attempt < retry_429_max_attempts:
                        delay = self._calc_retry_delay(
                            attempt=attempt,
                            base_delay=retry_429_base_delay_sec,
                            max_delay=retry_429_max_delay_sec,
                        )
                        attempt += 1
                        logger.warning(
                            "[mio_nai] 命中 429，%.2fs 后自动重试 (%s/%s)",
                            delay,
                            attempt,
                            retry_429_max_attempts,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(
                        f"HTTPError 429: 已重试 {retry_429_max_attempts} 次，仍被限流。"
                    ) from exc
                raise

            content_type = str(resp_headers.get("Content-Type", "")).lower()
            return self._extract_image_bytes(body, content_type)

    def _build_nai_payload(self, positive: str, negative: str) -> Tuple[Dict[str, Any], int]:
        configured_seed = int(self.state.get("seed", -1))
        seed = configured_seed if configured_seed >= 0 else random.randint(1, 2**31 - 1)
        model = str(self.state.get("nai_model", "nai-diffusion-4-5-full"))

        params = {
            "params_version": 3,
            "width": int(self.state.get("width", 832)),
            "height": int(self.state.get("height", 1216)),
            "scale": float(self.state.get("scale", 6.0)),
            "sampler": str(self.state.get("sampler", "k_euler_ancestral")),
            "noise_schedule": "karras",
            "steps": int(self.state.get("steps", 28)),
            "n_samples": 1,
            "seed": seed,
            "cfg_rescale": 0.0,
            "ucPreset": 0,
            "qualityToggle": False,
            "dynamic_thresholding": False,
            "controlnet_strength": 1,
            "legacy": False,
            "add_original_image": True,
            "legacy_v3_extend": False,
            "skip_cfg_above_sigma": None,
            "use_coords": False,
            "characterPrompts": [],
            "reference_image_multiple": [],
            "reference_information_extracted_multiple": [],
            "reference_strength_multiple": [],
            "negative_prompt": negative,
        }

        # 默认关闭 SMEA/DYN，避免中转或模型侧异常。
        params["sm"] = False
        params["sm_dyn"] = False

        # v4 / v4.5 模型建议显式传 v4 prompt 结构。
        if model.startswith("nai-diffusion-4"):
            params["v4_prompt"] = {
                "caption": {
                    "base_caption": positive,
                    "char_captions": [],
                },
                "use_coords": False,
                "use_order": True,
            }
            params["v4_negative_prompt"] = {
                "caption": {
                    "base_caption": negative,
                    "char_captions": [],
                }
            }

        payload = {
            "input": positive,
            "model": model,
            "action": "generate",
            "parameters": params,
        }
        return payload, seed

    async def _optimize_prompt_with_llm(
        self,
        description: str,
        group_id: str,
        origin: Any = None,
    ) -> str:
        if not self._to_bool(self.state.get("llm_enable", False)):
            return description

        tags_by_provider = await self._optimize_prompt_with_astrbot_provider(
            description=description,
            group_id=group_id,
            origin=origin,
        )
        if tags_by_provider:
            return tags_by_provider

        url = str(self.state.get("llm_api_url", "")).strip()
        if not url:
            return description

        system_prompt = str(self.state.get("llm_system_prompt", "")).strip()
        model = str(self.state.get("llm_model", "")).strip()
        timeout = int(self.state.get("llm_timeout_sec", 60))
        key = str(self.state.get("llm_api_key", "")).strip()

        if group_id and group_id in self.group_context_buffers:
            context_text = " | ".join(list(self.group_context_buffers[group_id])[-8:])
            user_content = (
                f"User request/context:\n{description}\n\n"
                f"Recent group context:\n{context_text}\n\n"
                "Return only concise English tags."
            )
        else:
            user_content = f"User request:\n{description}\n\nReturn only concise English tags."

        payload: Dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
        }
        if model:
            payload["model"] = model

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            status, resp_headers, body = await asyncio.to_thread(
                self._http_post_raw, url, headers, data, timeout
            )
            if status >= 400:
                raise RuntimeError(f"LLM API HTTP {status}")
            content_type = str(resp_headers.get("Content-Type", "")).lower()
            if "json" not in content_type:
                text = body.decode("utf-8", errors="ignore")
            else:
                llm_json = json.loads(body.decode("utf-8", errors="ignore"))
                text = self._extract_llm_content(llm_json)
            tags = self._sanitize_tags(text)
            return tags or description
        except Exception as exc:
            logger.warning(f"[mio_nai] LLM 优化失败，回退原描述: {exc}")
            return description

    async def _optimize_prompt_with_astrbot_provider(
        self,
        description: str,
        group_id: str,
        origin: Any = None,
    ) -> str:
        provider_id = str(self.state.get("llm_provider_id", "")).strip()
        if not provider_id and origin is not None:
            get_provider = getattr(self.context, "get_current_chat_provider_id", None)
            if callable(get_provider):
                try:
                    provider_id = await get_provider(origin)
                except Exception as exc:
                    logger.debug(f"[mio_nai] 无法获取当前聊天 provider，回退外部 LLM: {exc}")

        if not provider_id:
            return ""

        llm_generate = getattr(self.context, "llm_generate", None)
        if not callable(llm_generate):
            logger.warning("[mio_nai] 当前 AstrBot 版本不支持 context.llm_generate")
            return ""

        system_prompt = str(self.state.get("llm_system_prompt", "")).strip()
        if group_id and group_id in self.group_context_buffers:
            context_text = " | ".join(list(self.group_context_buffers[group_id])[-8:])
            user_content = (
                f"User request/context:\n{description}\n\n"
                f"Recent group context:\n{context_text}\n\n"
                "Return only concise English tags."
            )
        else:
            user_content = f"User request:\n{description}\n\nReturn only concise English tags."

        try:
            contexts = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_content),
            ]
            timeout_sec = max(5, int(self.state.get("llm_timeout_sec", 60)))
            logger.info(
                "[mio_nai] calling AstrBot provider for tags, provider=%s timeout=%ss",
                provider_id,
                timeout_sec,
            )
            llm_resp = await asyncio.wait_for(
                llm_generate(
                    chat_provider_id=provider_id,
                    contexts=contexts,
                ),
                timeout=timeout_sec,
            )
            raw_text = str(getattr(llm_resp, "completion_text", "") or "").strip()
            if not raw_text:
                raw_text = str(getattr(llm_resp, "text", "") or "").strip()
            tags = self._sanitize_tags(raw_text)
            return tags
        except asyncio.TimeoutError:
            logger.warning(
                "[mio_nai] AstrBot Provider LLM 超时，已回退外部 LLM/原描述。"
            )
            return ""
        except Exception as exc:
            logger.warning(f"[mio_nai] AstrBot Provider LLM 调用失败，回退外部 LLM: {exc}")
            return ""

    async def _auto_scheduler_loop(self):
        while True:
            await asyncio.sleep(15)
            if not self._to_bool(self.state.get("auto_enabled", False)):
                continue

            whitelist = self._normalized_groups(self.state.get("whitelist_group_ids", []))
            if not whitelist:
                continue

            now = time.time()
            for group_id in whitelist:
                origin = self.group_last_origin.get(group_id)
                if origin is None:
                    continue

                due = self.group_next_time.get(group_id)
                if due is None:
                    self.group_next_time[group_id] = now + self._next_auto_interval_seconds()
                    continue
                if now < due:
                    continue

                queue_limit = int(self.state.get("auto_queue_limit", 3))
                if self.draw_queue.qsize() >= queue_limit:
                    self.group_next_time[group_id] = now + 600
                    continue

                desc = self._build_auto_description(group_id)
                if not desc:
                    self.group_next_time[group_id] = now + self._next_auto_interval_seconds()
                    continue

                task = DrawTask(
                    origin=origin,
                    description=desc,
                    source="auto",
                    user_id="0",
                    group_id=group_id,
                    use_llm=True,
                    is_auto=True,
                )
                await self.draw_queue.put(task)
                self.group_next_time[group_id] = now + self._next_auto_interval_seconds()

    def _build_auto_description(self, group_id: str) -> str:
        mode = str(self.state.get("auto_mode", "context")).lower()
        if mode == "random":
            prompts = self.state.get("auto_random_prompts", [])
            if isinstance(prompts, list) and prompts:
                return str(random.choice(prompts))
            return ""

        window = int(self.state.get("auto_context_window", 20))
        buff = self.group_context_buffers.get(group_id)
        if not buff:
            return ""
        context_text = " | ".join(list(buff)[-window:])
        return (
            "Create anime image tags from this group chat context. "
            f"Context: {context_text}"
        )

    def _remember_group_context(self, group_id: str, text: str, origin: Any) -> None:
        if not self._is_group_allowed(group_id):
            return
        if group_id not in self.group_context_buffers:
            self.group_context_buffers[group_id] = deque(maxlen=60)
        cleaned = re.sub(r"\s+", " ", text).strip()
        if cleaned:
            self.group_context_buffers[group_id].append(cleaned)
        if origin is not None:
            self.group_last_origin[group_id] = origin
        if group_id not in self.group_next_time:
            self.group_next_time[group_id] = time.time() + self._next_auto_interval_seconds()

    def _should_assist_draw(self, event: AstrMessageEvent, text: str) -> bool:
        if not self._to_bool(self.state.get("enable_assist", True)):
            return False

        group_id = self._get_group_id(event)
        if group_id and not self._is_group_allowed(group_id):
            return False

        lowered = text.lower()
        bot_names = [str(x).lower() for x in self.state.get("bot_names", []) if str(x).strip()]
        has_name = any(name in lowered for name in bot_names)
        is_wake = bool(getattr(event, "is_at_or_wake_command", False))
        if not (has_name or is_wake):
            return False

        keywords = [str(x) for x in self.state.get("draw_keywords", [])]
        return any(keyword in text for keyword in keywords)

    def _extract_natural_description(self, text: str) -> str:
        cleaned = re.sub(r"\[CQ:at,qq=\d+\]", "", text, flags=re.IGNORECASE).strip()
        for name in self.state.get("bot_names", []):
            cleaned = cleaned.replace(str(name), " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,，。")

        patterns = [
            r"(?:内容是|画|绘|生成|来一张|来张|做一张|做张)\s*(?P<desc>.+)$",
            r"(?:帮我|给我)\s*(?P<desc>.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, cleaned)
            if match:
                desc = match.group("desc").strip(" ,，。")
                if desc:
                    return desc
        return cleaned

    def _extract_llm_content(self, payload: Any) -> str:
        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    msg = first.get("message")
                    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                        return msg.get("content", "")
                    if isinstance(first.get("text"), str):
                        return first.get("text", "")
            if isinstance(payload.get("output_text"), str):
                return payload.get("output_text", "")
            if isinstance(payload.get("response"), str):
                return payload.get("response", "")
            if isinstance(payload.get("result"), str):
                return payload.get("result", "")
        return ""

    def _sanitize_tags(self, text: str) -> str:
        temp = (text or "").strip()
        temp = re.sub(r"```[\s\S]*?```", "", temp)
        temp = temp.replace("，", ",").replace("；", ",").replace("\n", ",")
        temp = temp.replace("。", ",")
        chunks = [x.strip() for x in temp.split(",")]
        items: List[str] = []
        seen = set()
        for chunk in chunks:
            if not chunk:
                continue
            if len(chunk) > 80:
                continue
            if chunk not in seen:
                seen.add(chunk)
                items.append(chunk)
            if len(items) >= 80:
                break
        return ", ".join(items)

    def _extract_image_bytes(self, body: bytes, content_type: str) -> Tuple[bytes, str]:
        ct = (content_type or "").lower()
        if "application/zip" in ct or (len(body) >= 2 and body[:2] == b"PK"):
            return self._extract_first_image_from_zip(body)

        if "image/" in ct:
            ext = self._guess_ext_from_content_type(ct)
            return body, ext

        try:
            data = json.loads(body.decode("utf-8", errors="ignore"))
            b64 = self._find_base64_image(data)
            if b64:
                raw = base64.b64decode(b64)
                return raw, "png"
        except Exception:
            pass

        snippet = body[:180].decode("utf-8", errors="ignore")
        raise RuntimeError(f"无法识别的图片返回格式: {snippet}")

    def _extract_first_image_from_zip(self, data: bytes) -> Tuple[bytes, str]:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if not names:
                raise RuntimeError("ZIP 里没有图片")
            image_names = [
                name
                for name in names
                if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ]
            target = image_names[0] if image_names else names[0]
            raw = zf.read(target)
            ext = target.split(".")[-1].lower() if "." in target else "png"
            if ext == "jpeg":
                ext = "jpg"
            return raw, ext

    def _find_base64_image(self, data: Any) -> str:
        if isinstance(data, dict):
            for key in ("image", "images", "base64", "b64_json", "data", "result"):
                val = data.get(key)
                if isinstance(val, str):
                    return self._strip_data_uri_prefix(val)
                if isinstance(val, list) and val and isinstance(val[0], str):
                    return self._strip_data_uri_prefix(val[0])
            for value in data.values():
                found = self._find_base64_image(value)
                if found:
                    return found
        if isinstance(data, list):
            for item in data:
                found = self._find_base64_image(item)
                if found:
                    return found
        return ""

    def _strip_data_uri_prefix(self, value: str) -> str:
        if value.startswith("data:image"):
            idx = value.find(",")
            if idx >= 0:
                return value[idx + 1 :]
        return value

    def _guess_ext_from_content_type(self, content_type: str) -> str:
        ct = content_type.lower()
        if "jpeg" in ct:
            return "jpg"
        if "webp" in ct:
            return "webp"
        if "png" in ct:
            return "png"
        return "png"

    def _save_image_file(self, data: bytes, ext: str) -> Path:
        filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        path = self.cache_dir / filename
        path.write_bytes(data)
        return path

    def _http_post_raw(
        self, url: str, headers: Dict[str, str], data: bytes, timeout_sec: int
    ) -> Tuple[int, Dict[str, str], bytes]:
        req = Request(url=url, data=data, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)

        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                status = int(getattr(resp, "status", 200))
                resp_headers = dict(resp.headers.items())
                body = resp.read()
                return status, resp_headers, body
        except HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            snippet = body[:200].decode("utf-8", errors="ignore")
            headers = dict(exc.headers.items()) if getattr(exc, "headers", None) else {}
            server = str(headers.get("Server", "")).lower()
            cf_ray = str(headers.get("CF-RAY", "")).strip()
            if exc.code == 403 and (
                "cloudflare" in server or "<!doctype html" in snippet.lower()
            ):
                extra = f" CF-RAY={cf_ray}" if cf_ray else ""
                raise RuntimeError(
                    "HTTPError 403: 请求被 Cloudflare 拦截。"
                    " 请在 Cloudflare 对 /ai/generate-image 放行规则"
                    f"(跳过 WAF/Bot/Challenge)。{extra}"
                )
            raise RuntimeError(f"HTTPError {exc.code}: {snippet}")
        except URLError as exc:
            raise RuntimeError(f"网络错误: {exc}")

    async def _send_plain(self, origin: Any, text: str) -> None:
        if origin is None:
            return
        chain = MessageChain().message(str(text))
        await self._send_chain(origin, chain)

    async def _send_chain(self, origin: Any, chain: MessageChain) -> None:
        if origin is None:
            return
        try:
            await self.context.send_message(origin, chain)
        except Exception as exc:
            logger.warning(f"[mio_nai] 主动消息发送失败: {exc}")

    def _is_group_allowed(self, group_id: str) -> bool:
        allowed = self._normalized_groups(self.state.get("whitelist_group_ids", []))
        if not allowed:
            return True
        return str(group_id) in set(allowed)

    def _normalized_groups(self, val: Any) -> List[str]:
        if not isinstance(val, list):
            return []
        result: List[str] = []
        for item in val:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _next_auto_interval_seconds(self) -> int:
        min_m = max(1, int(self.state.get("auto_min_minutes", 30)))
        max_m = max(min_m, int(self.state.get("auto_max_minutes", 90)))
        return random.randint(min_m * 60, max_m * 60)

    def _queue_tip(self, mode_name: str, pending_before: int) -> str:
        started_tpl = str(self.state.get("tip_started_template", "")).strip()
        queued_tpl = str(self.state.get("tip_queued_template", "")).strip()
        if pending_before <= 0:
            return self._format_tip(
                template=started_tpl,
                fallback="{mode}已开始生成。",
                mode=mode_name,
                pending=0,
            )
        return self._format_tip(
            template=queued_tpl,
            fallback="{mode}已加入队列，前方还有 {pending} 个任务。",
            mode=mode_name,
            pending=pending_before,
        )

    def _format_tip(self, template: str, fallback: str, **kwargs: Any) -> str:
        base = template or fallback
        try:
            return base.format(**kwargs)
        except Exception:
            return fallback.format(**kwargs)

    def _is_http_429_error(self, error_text: str) -> bool:
        text = str(error_text or "")
        return text.startswith("HTTPError 429") or " HTTP 429" in text

    def _calc_retry_delay(self, attempt: int, base_delay: float, max_delay: float) -> float:
        core = min(max_delay, base_delay * (2**attempt))
        jitter = random.uniform(0.0, min(1.0, base_delay))
        return max(0.5, core + jitter)

    def _join_tags(self, parts: List[str]) -> str:
        tags: List[str] = []
        seen = set()
        for part in parts:
            if not part:
                continue
            normalized = part.replace("，", ",").replace("\n", ",")
            for item in normalized.split(","):
                tag = item.strip()
                if not tag:
                    continue
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
        return ", ".join(tags)

    def _payload_for_log(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(payload)
        result["input"] = self._trim_text_for_log(str(result.get("input", "")), 500)
        params = result.get("parameters")
        if isinstance(params, dict):
            params_copy = dict(params)
            if "negative_prompt" in params_copy:
                params_copy["negative_prompt"] = self._trim_text_for_log(
                    str(params_copy.get("negative_prompt", "")),
                    300,
                )
            result["parameters"] = params_copy
        return result

    def _trim_text_for_log(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + "...(truncated)"

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "on", "yes", "开", "开启"}
        return bool(value)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        is_admin_method = getattr(event, "is_admin", None)
        if callable(is_admin_method):
            try:
                if bool(is_admin_method()):
                    return True
            except Exception:
                pass

        user_id = self._get_sender_id(event)
        admin_ids = {str(x) for x in self.state.get("admin_user_ids", [])}
        if user_id and user_id in admin_ids:
            return True

        group_id = self._get_group_id(event)
        if group_id:
            role = self._get_sender_role(event)
            return role in {"admin", "owner"}

        if user_id and user_id in admin_ids:
            return True
        return False

    def _get_sender_role(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        sender = getattr(msg_obj, "sender", None)
        role = getattr(sender, "role", "")
        return str(role).lower()

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        method = getattr(event, "get_sender_id", None)
        if callable(method):
            try:
                val = method()
                if val is not None:
                    return str(val)
            except Exception:
                pass
        msg_obj = getattr(event, "message_obj", None)
        sender = getattr(msg_obj, "sender", None)
        user_id = getattr(sender, "user_id", None)
        if user_id is None:
            user_id = getattr(msg_obj, "user_id", None)
        return "" if user_id is None else str(user_id)

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        method = getattr(event, "get_group_id", None)
        if callable(method):
            try:
                val = method()
                if val is not None:
                    return str(val)
            except Exception:
                pass
        msg_obj = getattr(event, "message_obj", None)
        group_id = getattr(msg_obj, "group_id", None)
        return "" if group_id is None else str(group_id)

    def _manage_whitelist(self, args: List[str]) -> str:
        groups = self._normalized_groups(self.state.get("whitelist_group_ids", []))
        if not args:
            return (
                "用法: /画图管理 白名单 添加 <群号> | 删除 <群号> | 列表\n"
                f"当前: {', '.join(groups) if groups else '未限制'}"
            )

        action = args[0].lower()
        if action in {"列表", "list"}:
            return f"白名单群: {', '.join(groups) if groups else '未限制'}"

        if len(args) < 2:
            return "请提供群号。"

        gid = str(args[1]).strip()
        if action in {"添加", "add"}:
            if gid not in groups:
                groups.append(gid)
            self.state["whitelist_group_ids"] = groups
            self._save_runtime_state()
            return f"已添加白名单群: {gid}"
        if action in {"删除", "del", "remove"}:
            groups = [x for x in groups if x != gid]
            self.state["whitelist_group_ids"] = groups
            self._save_runtime_state()
            return f"已移除白名单群: {gid}"
        return "用法: /画图管理 白名单 添加 <群号> | 删除 <群号> | 列表"

    def _config_snapshot(self) -> str:
        whitelist = self._normalized_groups(self.state.get("whitelist_group_ids", []))
        return (
            "当前配置:\n"
            f"- 模型: {self.state.get('nai_model')}\n"
            f"- NAI URL: {self.state.get('nai_api_url')}\n"
            f"- 429 重试: {'开' if self._to_bool(self.state.get('retry_429_enabled')) else '关'} "
            f"(最多 {self.state.get('retry_429_max_attempts')} 次)\n"
            f"- 自动画图: {'开启' if self._to_bool(self.state.get('auto_enabled')) else '关闭'} "
            f"({self.state.get('auto_mode')})\n"
            f"- 自动间隔: {self.state.get('auto_min_minutes')}~{self.state.get('auto_max_minutes')} 分钟\n"
            f"- LLM 优化: {'开启' if self._to_bool(self.state.get('llm_enable')) else '关闭'}\n"
            f"- LLM Provider: {self.state.get('llm_provider_id') or '跟随当前会话'}\n"
            f"- 请求日志: {'开' if self._to_bool(self.state.get('log_request_payload')) else '关'}\n"
            f"- 白名单群: {', '.join(whitelist) if whitelist else '未限制'}\n"
            f"- 基础画图标签校验: {'开' if self._to_bool(self.state.get('basic_draw_require_tags')) else '关'}\n"
            f"- 回显: {'开' if self._to_bool(self.state.get('echo_meta')) else '关'}"
        )

    def _manage_help_text(self) -> str:
        return (
            "画图管理命令:\n"
            "/画图管理 查看\n"
            "/画图管理 白名单 添加 <群号>\n"
            "/画图管理 白名单 删除 <群号>\n"
            "/画图管理 自动 开|关\n"
            "/画图管理 模式 上下文|随机\n"
            "/画图管理 间隔 <最小分钟> <最大分钟>\n"
            "/画图管理 正面 <tags>\n"
            "/画图管理 负面 <tags>\n"
            "/画图管理 追加 <tags>\n"
            "/画图管理 回显 开|关\n"
            "/画图管理 opus 开|关\n"
            "/画图管理 请求日志 开|关\n"
            "/画图管理 文案开始 <文案>\n"
            "/画图管理 文案排队 <文案>\n"
            "/画图管理 基础警告 <文案>\n"
            "/画图管理 api <url>\n"
            "/画图管理 key <nai_api_key>\n"
            "/画图管理 model <nai_model>\n"
            "/画图管理 llm开 | llm关\n"
            "/画图管理 llmprovider <provider_id>\n"
            "/画图管理 llmurl <url>\n"
            "/画图管理 llmkey <key>\n"
            "/画图管理 llmmodel <model>"
        )

    def _as_list(self, value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        if isinstance(value, str):
            if not value.strip():
                return []
            items = re.split(r"[,，\n\r]+", value)
            return [x.strip() for x in items if x.strip()]
        return []

    def _collect_config_values(
        self, node: Any, valid_keys: Set[str], out: Dict[str, Any]
    ) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in valid_keys and not isinstance(value, dict):
                out[key] = value
            if isinstance(value, dict):
                self._collect_config_values(value, valid_keys, out)

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from .conversation_store import ConversationStore, DEFAULT_DB_PATH
from .data import load_examples
from .local_model import generate_review, load_local_model
from .review_team import review_all_roles
from .roles import ROLES, TONE_MODES, role_by_id
from .voice_ui import role_voice_card_html


ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

BASE_MODEL = os.getenv("PRGUARD_BASE_MODEL", str(ROOT / "models" / "base" / "Qwen3-1.7B"))
ADAPTER = os.getenv("PRGUARD_ADAPTER", str(ROOT / "outputs" / "adapters" / "by-role-selected"))
FALLBACK_ADAPTER = os.getenv(
    "PRGUARD_FALLBACK_ADAPTER", str(ROOT / "outputs" / "adapters" / "by-role")
)
OUTPUT_MODE = os.getenv("PRGUARD_OUTPUT_MODE", "evidence_line_id").strip().lower()
EVIDENCE_AWARE_MODE = OUTPUT_MODE == "evidence_line_id"
LEGACY_ROLE_IDS = tuple(
    role_id.strip()
    for role_id in os.getenv("PRGUARD_LEGACY_ROLE_IDS", "test_reviewer").split(",")
    if role_id.strip()
) if EVIDENCE_AWARE_MODE else ()
ROLE_BY_ID = {role.role_id: role for role in ROLES}


@st.cache_resource(show_spinner=False)
def cached_local_model(
    base_model: str,
    adapter_path: str,
    fallback_adapter_path: str,
    legacy_role_ids: tuple[str, ...],
):
    return load_local_model(
        base_model,
        adapter_path,
        legacy_adapter_root=fallback_adapter_path or None,
        legacy_role_ids=legacy_role_ids,
    )


@st.cache_resource(show_spinner=False)
def cached_conversation_store(db_path: str) -> ConversationStore:
    return ConversationStore(db_path)


@st.cache_data(show_spinner=False)
def cached_training_examples() -> list[dict[str, Any]]:
    return load_examples("train", limit=2500)


def _conversation_label(row: dict[str, Any]) -> str:
    updated = str(row.get("updated_at", ""))[:16].replace("T", " ")
    count = int(row.get("message_count", 0))
    return f"{row['title']} · {updated} · {count} 条消息"


def _assistant_speech_text(validation: dict[str, Any]) -> str:
    if validation.get("status") != "evidence_quote_match":
        return ""
    parts: list[str] = []
    for finding in validation.get("findings", []):
        comment = finding.get("review_comment") or finding.get("finding") or ""
        impact = finding.get("impact") or ""
        fix = finding.get("suggested_fix") or ""
        parts.extend(str(value).strip() for value in (comment, impact, fix) if str(value).strip())
    return "。".join(parts)


def _show_assistant_message(message: dict[str, Any]) -> None:
    role = ROLE_BY_ID.get(message.get("speaker_id"))
    validation = message.get("validation") or {}
    metadata = message.get("metadata") or {}
    if role:
        st.markdown(f"**{role.persona_name} · {role.name}**")

    if validation.get("status") == "evidence_quote_match":
        st.caption(
            "证据 ID 已映射到输入 diff 的变更行。门禁只核验结构和引用，评论语义仍需结合上下文阅读。"
            if validation.get("check_scope") == "role_json_and_exact_changed_line_id"
            else "旧 exact-quote 门禁已核对 diff 原文引用；这不等于判断语义正确。"
        )
        for finding in validation.get("findings", []):
            comment = finding.get("review_comment") or finding.get("finding") or ""
            if comment:
                st.markdown(comment)
            if finding.get("impact"):
                st.write(f"影响：{finding['impact']}")
            if finding.get("suggested_fix"):
                st.write(f"建议：{finding['suggested_fix']}")
            evidence = finding.get("evidence", {})
            if evidence:
                location = ":".join(
                    str(value)
                    for value in (evidence.get("file_path", ""), evidence.get("line_number", ""))
                    if value != ""
                )
                st.caption(
                    f"证据 {finding.get('evidence_id', '')} · {location} · {evidence.get('side', '')}"
                )
                st.code(evidence.get("snippet") or evidence.get("evidence") or "")
        if not validation.get("findings"):
            st.info(validation.get("abstain_reason") or "当前角色没有报告有证据支持的问题。")
    elif validation.get("status") == "abstained":
        st.info(validation.get("abstain_reason") or "当前 diff 没有可引用的变更行，角色选择暂不提出问题。")
    else:
        reasons = ", ".join(validation.get("reasons", ["validation_not_run"]))
        st.warning(f"该草稿没有通过输出校验：{reasons}。请只作为未验证文本阅读。")
        with st.expander("查看原始草稿"):
            st.text(message.get("content", ""))

    sources = metadata.get("guidance_sources") or []
    if sources:
        st.caption("知识片段：" + "；".join(sources))
    if role:
        speech = _assistant_speech_text(validation)
        if speech:
            components.html(role_voice_card_html(role, speech), height=102, scrolling=False)


def _render_history(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        if message["speaker_type"] == "user":
            with st.chat_message("user"):
                st.markdown(message["content"])
        elif message["speaker_type"] == "assistant":
            with st.chat_message("assistant"):
                _show_assistant_message(message)


def run_app() -> None:
    st.set_page_config(page_title="PRGuard 多角色对话", page_icon="🛡️", layout="wide")
    st.title("PRGuard 多角色代码评审对话")
    st.caption("围绕同一个 Issue 和 diff 连续追问；可以让五个角色协作，也可以切换到单个角色继续讨论。")

    db_path = os.getenv("PRGUARD_MEMORY_DB", str(DEFAULT_DB_PATH))
    store = cached_conversation_store(db_path)
    conversations = store.list_conversations()
    ids = [row["id"] for row in conversations]
    active_id = st.session_state.get("active_chat_id")
    if active_id not in ids:
        active_id = None
        st.session_state["active_chat_id"] = None

    role_names = {role.role_id: f"{role.persona_name} · {role.name}" for role in ROLES}
    selected_index = ids.index(active_id) + 1 if active_id in ids else 0

    with st.sidebar:
        st.header("新建对话")
        draft_title = st.text_input("对话标题（可选）", key="draft_title")
        draft_issue = st.text_area(
            "Issue / 验收要求",
            height=100,
            placeholder="描述本次变更要解决的问题和验收条件……",
            key="draft_issue",
        )
        draft_diff = st.text_area(
            "Pull Request diff",
            height=190,
            placeholder="粘贴 git diff 或关键变更片段……",
            key="draft_diff",
        )
        stored_conversation = store.get_conversation(active_id) if active_id else None
        stored_tone = (
            stored_conversation.get("tone_mode", "中性专业")
            if stored_conversation
            else "中性专业"
        )
        tone_key = f"tone_{active_id or 'new'}"
        if tone_key not in st.session_state:
            st.session_state[tone_key] = stored_tone
        tone_mode = st.selectbox(
            "表达语气",
            options=list(TONE_MODES),
            index=list(TONE_MODES).index(st.session_state[tone_key]),
            key=tone_key,
        )
        if st.button("以此背景开启新对话", type="primary", use_container_width=True):
            if not draft_diff.strip():
                st.warning("请先填写 PR diff。")
            else:
                new_id = store.create_conversation(
                    draft_issue,
                    draft_diff,
                    title=draft_title.strip() or None,
                    tone_mode=tone_mode,
                )
                st.session_state["active_chat_id"] = new_id
                st.rerun()

        st.divider()
        st.header("历史对话")
        conversations = store.list_conversations()
        id_to_row = {row["id"]: row for row in conversations}
        choice = st.selectbox(
            "选择要继续的对话",
            options=[None, *id_to_row],
            index=selected_index if selected_index <= len(id_to_row) else 0,
            format_func=lambda value: "新建对话" if value is None else _conversation_label(id_to_row[value]),
        )
        if choice != st.session_state.get("active_chat_id"):
            st.session_state["active_chat_id"] = choice
            st.rerun()
        if choice and st.button("删除当前对话", use_container_width=True):
            store.delete_conversation(choice)
            st.session_state["active_chat_id"] = None
            st.rerun()

        st.divider()
        st.header("本轮参与角色")
        selected_role_ids = st.multiselect(
            "选择一个角色或多个角色",
            options=[role.role_id for role in ROLES],
            default=[role.role_id for role in ROLES],
            format_func=lambda role_id: role_names[role_id],
        )
        st.caption("多个角色会按专业顺序发言；后续角色可参考前面角色已通过校验的意见。")

        if EVIDENCE_AWARE_MODE and LEGACY_ROLE_IDS:
            fallback_names = "、".join(
                ROLE_BY_ID[role_id].persona_name
                for role_id in LEGACY_ROLE_IDS
                if role_id in ROLE_BY_ID
            )
            st.info(f"{fallback_names or '指定角色'}使用旧 Adapter；其他角色使用证据感知 Adapter。")
        else:
            st.caption("输出协议：" + ("diff 行 ID 证据" if EVIDENCE_AWARE_MODE else "旧 findings exact-quote"))
        st.caption("模型和对话历史保存在本机；PRGuard 不会将 PR 评论写回 GitHub。")

    active_id = st.session_state.get("active_chat_id")
    conversation = store.get_conversation(active_id) if active_id else None
    if conversation:
        if tone_mode != conversation["tone_mode"]:
            store.update_tone(active_id, tone_mode)
            conversation["tone_mode"] = tone_mode
        st.subheader(conversation["title"])
        with st.expander("本次评审背景", expanded=False):
            st.markdown(conversation["issue"] or "未填写 Issue")
            st.code(conversation["diff"], language="diff")
        with st.expander("五个角色的人设", expanded=False):
            for role in ROLES:
                st.markdown(f"**{role.persona_name} · {role.name}**")
                st.write(f"性格：{role.personality}")
                st.write(f"专业背景：{role.professional_background}")
                st.write(f"经历原型：{role.experience_story}")
                st.caption(f"说话方式：{role.speaking_style}")
    else:
        st.info("先在左侧填写 Issue 和 diff 并开启对话，再在下方输入问题。")

    messages = store.get_messages(active_id) if active_id else []
    _render_history(messages)

    user_message = st.chat_input(
        "继续追问、指定边界场景，或请角色解释自己的判断……",
        disabled=not conversation or not selected_role_ids,
    )
    if user_message and conversation and selected_role_ids:
        store.update_tone(active_id, tone_mode)
        store.append_message(
            active_id,
            speaker_type="user",
            speaker_id="user",
            content=user_message,
        )
        history = store.get_messages(active_id)[:-1]
        training_examples = cached_training_examples()
        try:
            with st.spinner("角色正在读取对话记忆并依次回应……"):
                tokenizer, model = cached_local_model(
                    BASE_MODEL,
                    ADAPTER,
                    FALLBACK_ADAPTER if LEGACY_ROLE_IDS else "",
                    LEGACY_ROLE_IDS,
                )

                def generate(**kwargs: Any) -> str:
                    return generate_review(
                        tokenizer,
                        model,
                        system_prompt=kwargs["system_prompt"],
                        user_prompt=kwargs["user_prompt"],
                        adapter_name=kwargs.get("adapter_name"),
                        deterministic=EVIDENCE_AWARE_MODE,
                    )

                results = review_all_roles(
                    conversation["issue"],
                    conversation["diff"],
                    generate,
                    training_examples=training_examples,
                    evidence_aware=EVIDENCE_AWARE_MODE,
                    legacy_role_ids=frozenset(LEGACY_ROLE_IDS),
                    selected_role_ids=selected_role_ids,
                    current_message=user_message,
                    conversation_history=history,
                    tone_mode=tone_mode,
                )
                for result in results:
                    store.append_message(
                        active_id,
                        speaker_type="assistant",
                        speaker_id=result["role_id"],
                        content=result["response"],
                        validation=result["validation"],
                        metadata={
                            "guidance_sources": result["guidance_sources"],
                            "example_sources": result["example_sources"],
                            "tone_mode": tone_mode,
                        },
                    )
            st.rerun()
        except Exception as exc:
            st.error(f"本轮生成失败：{exc}")

    with st.expander("五个专业角色", expanded=False):
        for role in ROLES:
            st.markdown(f"**{role.persona_name} · {role.name}**")
            st.write(role.personality)
            st.write(role.professional_background)
            st.write(role.experience_story)
            st.caption(f"说话方式：{role.speaking_style}")



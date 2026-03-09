"""TelegramFormatter — format GNOT events as Telegram messages.

Supports full and compact styles, Vietnamese and English.
Zero GNOT runtime imports — works with plain dicts from GNOT API responses.
"""

from __future__ import annotations

import time
from typing import Any


class TelegramFormatter:
    """Format GNOT events/responses as Telegram messages.

    Supports:
      - format_question():          Q&A notification (Pattern 1)
      - format_intent_response():   /intent API response (Pattern 2)
      - format_session_status():    /status command output
      - format_suspension_notice(): Task suspended mid-conversation

    style: "full" (default) | "compact"
    language: "vi" | "en"
    """

    def __init__(self, style: str = "full", language: str = "vi") -> None:
        self.style = style
        self.language = language

    # ── Pattern 1 — Q&A notification ──────────────────────────────────────

    def format_question(
        self,
        question_id: str,
        question_text: str,
        required_role: str,
        source_agent: str,
        cluster_id: str,
        timeout_hours: int = 24,
        default_assumption: str | None = None,
    ) -> str:
        """Format Q&A notification for Pattern 1 (transactional Q&A)."""
        if self.style == "compact":
            return self._format_question_compact(
                question_id, question_text, required_role, source_agent
            )
        return self._format_question_full(
            question_id, question_text, required_role, source_agent,
            cluster_id, timeout_hours, default_assumption,
        )

    def _format_question_full(
        self, question_id: str, question_text: str,
        required_role: str, source_agent: str,
        cluster_id: str, timeout_hours: int,
        default_assumption: str | None,
    ) -> str:
        if self.language == "vi":
            lines = [
                f"🤔 *Câu hỏi từ agent `{source_agent}`*",
                f"Vai trò cần thiết: `{required_role}`",
                f"Cluster: `{cluster_id}`",
                "",
                question_text,
                "",
                "💬 Reply tin nhắn này để trả lời.",
                f"⏰ Timeout: {timeout_hours}h",
            ]
        else:
            lines = [
                f"🤔 *Question from agent `{source_agent}`*",
                f"Role needed: `{required_role}`",
                f"Cluster: `{cluster_id}`",
                "",
                question_text,
                "",
                "💬 Reply to this message to answer.",
                f"⏰ Timeout: {timeout_hours}h",
            ]
        if default_assumption:
            lines.append(f"📌 Assumption: {default_assumption}")
        lines.append(f"🔑 `{question_id}`")
        return "\n".join(lines)

    def _format_question_compact(
        self, question_id: str, question_text: str,
        required_role: str, source_agent: str,
    ) -> str:
        prefix = "❓" if self.language == "vi" else "❓"
        return f"{prefix} [{required_role}@{source_agent}] {question_text}\n`{question_id}`"

    # ── Pattern 2 — Intent response ────────────────────────────────────────

    def format_intent_response(self, response: dict) -> str:
        """Format /intent API response for display in Telegram.

        Handles: normal reply, suspended (question asked), error.
        """
        # Suspended — agent asked a question
        if response.get("suspended") or response.get("status") == "suspended":
            question = response.get("question") or response.get("question_text", "")
            if self.language == "vi":
                return f"⏸ *Task tạm dừng — chờ input*\n❓ {question}"
            return f"⏸ *Task suspended — waiting for input*\n❓ {question}"

        # Normal reply
        reply = response.get("reply") or response.get("message", "")
        if not reply:
            if self.language == "vi":
                return "✅ Hoàn thành (không có nội dung trả lời)"
            return "✅ Done (no reply content)"
        return reply

    # ── /status command ────────────────────────────────────────────────────

    def format_session_status(
        self,
        session_id: str,
        session: dict | None,
        tasks: list[dict],
    ) -> str:
        """Format /status command output.

        Shows session info + any suspended tasks.
        """
        if session is None:
            if self.language == "vi":
                return f"❌ Session `{session_id}` không tồn tại."
            return f"❌ Session `{session_id}` not found."

        lines = [f"📊 *Session `{session_id}`*"]

        # Last activity
        last_activity = session.get("updated_at") or session.get("created_at")
        if last_activity:
            ago = _format_time_ago(last_activity)
            if self.language == "vi":
                lines.append(f"🕐 Hoạt động cuối: {ago}")
            else:
                lines.append(f"🕐 Last activity: {ago}")

        # Last message
        messages = session.get("messages", [])
        if messages:
            last_msg = messages[-1]
            role = last_msg.get("role", "")
            content = last_msg.get("content", "")
            if len(content) > 80:
                content = content[:77] + "..."
            lines.append(f"💬 `{role}: {content}`")

        # Suspended tasks
        suspended = [t for t in tasks if t.get("status") == "suspended"]
        if suspended:
            t = suspended[0]
            question = t.get("question") or t.get("pending_question", {}).get("question", "")
            role = t.get("pending_question", {}).get("required_role", "?")
            if self.language == "vi":
                lines.append(f"\n⏸ *Đang đợi:* vai trò `{role}`")
                lines.append(f"❓ {question}")
                lines.append("\nReply để trả lời, hoặc gửi tin nhắn mới để tiếp tục.")
            else:
                lines.append(f"\n⏸ *Suspended:* waiting for role `{role}`")
                lines.append(f"❓ {question}")
                lines.append("\nReply to answer, or send a new message to continue.")
        else:
            if self.language == "vi":
                lines.append("✅ Đang chạy")
            else:
                lines.append("✅ Running")

        return "\n".join(lines)

    # ── Suspension notice (mid-conversation) ───────────────────────────────

    def format_suspension_notice(self, suspended_response: dict) -> str:
        """Format notification when task gets suspended mid-conversation."""
        question = suspended_response.get("question") or suspended_response.get("question_text", "")
        role = suspended_response.get("required_role", "")

        if self.language == "vi":
            lines = ["⏸ *Task tạm dừng*"]
            if role:
                lines.append(f"Đang đợi input từ vai trò: `{role}`")
            if question:
                lines.append(f"❓ {question}")
            lines.append("\nReply để trả lời câu hỏi này.")
        else:
            lines = ["⏸ *Task suspended*"]
            if role:
                lines.append(f"Waiting for input from role: `{role}`")
            if question:
                lines.append(f"❓ {question}")
            lines.append("\nReply to answer this question.")

        return "\n".join(lines)

    # ── Conversation link confirmation ─────────────────────────────────────

    def format_conversation_linked(
        self,
        session_id: str,
        session: dict | None,
        tasks: list[dict],
    ) -> str:
        """Format the confirmation message after /conversation <session_id>."""
        if self.language == "vi":
            lines = [f"✅ Đã liên kết với cuộc hội thoại `{session_id}`"]
        else:
            lines = [f"✅ Linked to conversation `{session_id}`"]

        if session:
            last_activity = session.get("updated_at") or session.get("created_at")
            if last_activity:
                ago = _format_time_ago(last_activity)
                if self.language == "vi":
                    lines.append(f"📊 Hoạt động cuối: {ago}")
                else:
                    lines.append(f"📊 Last activity: {ago}")

            messages = session.get("messages", [])
            if messages:
                last_msg = messages[-1]
                content = last_msg.get("content", "")
                if len(content) > 80:
                    content = content[:77] + "..."
                lines.append(f"💬 `{content}`")

        suspended = [t for t in tasks if t.get("status") == "suspended"]
        if suspended:
            t = suspended[0]
            question = t.get("question") or t.get("pending_question", {}).get("question", "")
            role = t.get("pending_question", {}).get("required_role", "?")
            if self.language == "vi":
                lines.append(f"\n⏸ Đang đợi vai trò `{role}`")
                lines.append(f"❓ {question}")
                lines.append("\nReply để trả lời, hoặc gửi tin nhắn mới để tiếp tục.")
            else:
                lines.append(f"\n⏸ Suspended: waiting for role `{role}`")
                lines.append(f"❓ {question}")
                lines.append("\nReply to answer, or send a new message to continue.")
        else:
            if self.language == "vi":
                lines.append("⏳ Trạng thái: đang chạy")
                lines.append("\nBạn có thể tiếp tục cuộc hội thoại tại đây.")
            else:
                lines.append("⏳ Status: running")
                lines.append("\nYou can now continue the conversation here.")

        return "\n".join(lines)

    def format_start(self, bot_username: str) -> str:
        """Format /start welcome message."""
        if self.language == "vi":
            return (
                f"👋 Xin chào! Tôi là *{bot_username}*, cầu nối Telegram cho GNOT.\n\n"
                "**Lệnh hỗ trợ:**\n"
                "`/conversation <session_id>` — liên kết Telegram này với một GNOT session\n"
                "`/session` — xem session đang liên kết\n"
                "`/status` — trạng thái session + task đang chờ\n"
                "`/end` — hủy liên kết session\n"
                "`/register <role>` — đăng ký là participant cho role\n\n"
                "Sau khi liên kết session, bạn có thể chat bình thường để tương tác với agent."
            )
        return (
            f"👋 Hello! I'm *{bot_username}*, the Telegram bridge for GNOT.\n\n"
            "**Available commands:**\n"
            "`/conversation <session_id>` — link this chat to a GNOT session\n"
            "`/session` — show linked session\n"
            "`/status` — session status + pending tasks\n"
            "`/end` — unlink session\n"
            "`/register <role>` — register as a participant for a role\n\n"
            "Once linked, send any message to interact with the agent."
        )


# ── helpers ─────────────────────────────────────────────────────────────────

def _format_time_ago(timestamp: float) -> str:
    """Format a unix timestamp as a human-readable 'X ago' string."""
    delta = time.time() - timestamp
    if delta < 60:
        return "just now"
    elif delta < 3600:
        mins = int(delta / 60)
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    elif delta < 86400:
        hours = int(delta / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = int(delta / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"

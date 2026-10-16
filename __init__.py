"""Hermes plugin for hermes-mail.

The tools talk to the hermes-maild socket. The service owns the IMAP
connections and the sign-in tokens; this plugin never sees a token.
"""

from __future__ import annotations

from pathlib import Path

from . import tools, triage


def register(ctx) -> None:
    tools.configure(ctx.get_config("socket", ""), lambda: ctx.get_config("notify", {}))
    for name, schema, handler, emoji in tools.TOOLS:
        ctx.register_tool(
            name=name,
            toolset=tools.TOOLSETS.get(name, "mail"),
            schema=schema,
            handler=handler,
            check_fn=tools.available,
            emoji=emoji,
        )
    skill = Path(__file__).parent / "skills" / "mail" / "SKILL.md"
    ctx.register_skill("mail", skill, description="Read, search and manage recent mail with the mail_* tools.")
    ctx.register_auxiliary_task(
        triage.TASK_KEY,
        display_name="Mail triage",
        description="Classifies new mail and writes the notification summary for hermes-mail.",
        defaults={"timeout": 180},
    )
    triage.add_cli(ctx, lambda: tools._socket)

import discord


def safe_discord_text(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))[:100]


def bounded_discord_message(lines: list[str], *, limit: int = 1900) -> str:
    selected: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + (1 if selected else 0)
        if length + addition > limit:
            while True:
                omitted = len(lines) - len(selected)
                suffix = f"… {omitted}개 항목 생략"
                prefix = "\n".join(selected)
                candidate = f"{prefix}\n{suffix}" if prefix else suffix
                if len(candidate) <= limit:
                    return candidate
                if not selected:
                    return suffix[:limit]
                selected.pop()
        selected.append(line)
        length += addition
    return "\n".join(selected)

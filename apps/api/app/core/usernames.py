import re

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def normalize_username(value: str) -> str:
    value = str(value or "").strip()
    if not 2 <= len(value) <= 64:
        raise ValueError("username must be 2-64 characters")
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("username may contain only letters, numbers, dots, dashes and underscores")
    return value

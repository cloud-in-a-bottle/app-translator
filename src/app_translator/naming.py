import re

_VALID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def sanitize_app_name(raw: str) -> str:
    """Coerce an arbitrary name into openhost's ^[a-z][a-z0-9_-]{0,31}$ shape.

    Raises ValueError if nothing usable is left, rather than inventing a name.
    """
    lowered = raw.strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", lowered).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if cleaned and not cleaned[0].isalpha():
        cleaned = f"app-{cleaned}"
    cleaned = cleaned[:32].rstrip("-_")
    if not cleaned or not _VALID.match(cleaned):
        raise ValueError(f"cannot derive a valid openhost app name from {raw!r}")
    return cleaned


def is_valid_app_name(name: str) -> bool:
    return bool(_VALID.match(name))

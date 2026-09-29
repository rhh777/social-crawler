from .models import Item


def assess(item: Item, *, detail=False) -> Item:
    data = item.data
    value = data.get("published_at")
    try:
        stamp = float(value)
        data["published_at"] = stamp / 1000 if stamp > 100_000_000_000 else stamp
    except (TypeError, ValueError):
        data["published_at"] = None
    missing = []
    if not isinstance(data.get("text"), str):
        missing.append("text")
    if not data.get("author_id"):
        missing.append("author_id")
    if not data.get("published_at") or data["published_at"] <= 0:
        missing.append("published_at")
    data["quality"] = {
        "missing_required": missing,
        "text_state": "missing"
        if data.get("text") is None
        else "empty"
        if data["text"] == ""
        else "present",
    }
    if item.kind == "content":
        data["detail_observed"] = detail
        data["detail_complete"] = detail and not missing
    return item

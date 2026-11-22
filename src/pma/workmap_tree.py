"""Display current Work IDs and names from a validated Map."""


def id_title_tree(snapshot: dict) -> str:
    """Render current members only, using one validated Store revision."""
    display = snapshot["display_ids"]
    parents = {item["child"]: item["parent"] for item in snapshot["map"]["relations"] if item["kind"] == "parent"}
    children: dict[str | None, list[str]] = {}
    for key in snapshot["map"]["members"]:
        children.setdefault(parents.get(key), []).append(key)
    order = lambda key: tuple(int(part) for part in display[key][1:].split("."))
    for group in children.values():
        group.sort(key=order)
    lines: list[str] = []

    def visit(key: str, prefix: str, last: bool, root: bool = False) -> None:
        marker = "" if root else ("└─ " if last else "├─ ")
        lines.append(f"{prefix}{marker}{display[key]}  {snapshot['works'][key]['name']}")
        descendants = children.get(key, [])
        next_prefix = prefix + ("" if root else ("   " if last else "│  "))
        for index, child in enumerate(descendants):
            visit(child, next_prefix, index == len(descendants) - 1)

    for root in children.get(None, []):
        visit(root, "", True, True)
    return "\n".join(lines)

from config import MAX_FILE_READ_BYTES, MAX_FILE_WRITE_CHARS, WORKSPACE_ROOT


def resolve_workspace_path(path: str):
    from pathlib import Path
    target = (WORKSPACE_ROOT / path).resolve()
    if WORKSPACE_ROOT not in target.parents and target != WORKSPACE_ROOT:
        raise PermissionError("Path traversal blocked")
    return target


async def file_write(path: str, content: str) -> dict:
    if len(content) > MAX_FILE_WRITE_CHARS:
        raise ValueError("Content too large")
    safe_path = resolve_workspace_path(path)
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    safe_path.write_text(content, encoding="utf-8")
    return {"written_bytes": len(content), "path": str(safe_path.relative_to(WORKSPACE_ROOT))}


async def file_read(path: str) -> dict:
    safe_path = resolve_workspace_path(path)
    if safe_path.stat().st_size > MAX_FILE_READ_BYTES:
        raise ValueError("File too large")
    return {
        "content": safe_path.read_text(encoding="utf-8"),
        "path": str(safe_path.relative_to(WORKSPACE_ROOT)),
    }

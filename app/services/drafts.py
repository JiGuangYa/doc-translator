"""Read native-owned draft snapshots without loading entire documents in Swift."""
import json

from .. import config


def for_task(task_id: str) -> dict[str, str]:
    path = config.DATA_DIR / "revision-drafts.json"
    if not path.exists():
        return {}
    try:
        snapshot = json.loads(path.read_text())
        values = (snapshot.get("tasks", {}).get(task_id, {}).get("segments", {})
                  if snapshot.get("schema_version") == 2 else snapshot.get(task_id, {}))
        return {key: value for key, value in values.items() if isinstance(value, str)}
    except (OSError, ValueError, AttributeError) as error:
        raise ValueError("本机草稿暂不可读，请恢复草稿文件后重试") from error

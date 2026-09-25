from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

def load(path: Path):
    if not path.exists(): return {"repositories":{}, "updated_at":None}
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return {"repositories":{}, "updated_at":None}
def save(path: Path, state: dict):
    path.parent.mkdir(parents=True,exist_ok=True); state["updated_at"]=datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(state,indent=2),encoding="utf-8")

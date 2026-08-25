"""
Append-only, hash-chained audit log.

Every record carries the SHA-256 of the previous record. Altering or deleting
any earlier entry breaks the chain from that point forward, and `verify()`
reports exactly where. Combined with git history (each scan is a commit), a
prediction cannot be quietly rewritten after the outcome is known.

Records are never updated in place. A position's later fate is written as a
NEW record referencing the original decision id.
"""
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional

GENESIS = "0" * 64


def _canon(obj: Dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev: str, body: Dict) -> str:
    return hashlib.sha256((prev + _canon(body)).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            open(path, "a").close()

    def _last_hash(self) -> str:
        last = None
        for rec in self.read():
            last = rec
        return last["hash"] if last else GENESIS

    def append(self, event_type: str, payload: Dict,
               decision_id: Optional[str] = None) -> Dict:
        prev = self._last_hash()
        body = {
            "id": decision_id or str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": payload,
            "prev_hash": prev,
        }
        body["hash"] = _hash(prev, {k: v for k, v in body.items() if k != "hash"})
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(_canon(body) + "\n")
        return body

    def read(self) -> Iterator[Dict]:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    def verify(self) -> Dict:
        prev = GENESIS
        n = 0
        for i, rec in enumerate(self.read()):
            n += 1
            body = {k: v for k, v in rec.items() if k != "hash"}
            if rec.get("prev_hash") != prev or _hash(prev, body) != rec.get("hash"):
                return {"ok": False, "records": n, "broken_at": i,
                        "detail": f"chain break at record {i} ({rec.get('event_type')})"}
            prev = rec["hash"]
        return {"ok": True, "records": n, "broken_at": None, "detail": "chain intact"}

    def by_type(self, event_type: str) -> List[Dict]:
        return [r for r in self.read() if r.get("event_type") == event_type]

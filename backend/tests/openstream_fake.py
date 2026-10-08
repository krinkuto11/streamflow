"""A fake OpenStream control plane for hub/monitor tests: stands in for the
hub's requests.Session and records every call."""

from unittest.mock import Mock

import requests


def resp(status, body=None):
    r = Mock(status_code=status)
    r.json.return_value = body if body is not None else {}
    return r


class FakeOpenStream:
    def __init__(self, leases=True):
        self.snapshots = {}  # cid -> snapshot dict
        self.leases = {}     # cid -> set(owner)
        self.removed = []
        self.calls = []      # (method, path, params_or_json, headers)
        self.status = 200    # forced status for GET (401/403/428/500)
        self.down = False
        self.supports_leases = leases

    def healthy(self, cid, **health):
        h = {"state": "healthy", "keepUpMargin": 1.0, "reliabilityScore": 0.9, "confidence": 1.0}
        h.update(health)
        self.snapshots[cid] = {"contentID": cid, "peers": 10, "seeders": 4, "kbps": 5000, "health": h}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        if self.down:
            raise requests.ConnectionError("down")
        if self.status != 200:
            return resp(self.status, {"error": "x"})
        want = set((params or {}).get("ids", "").split(","))
        return resp(200, [s for c, s in self.snapshots.items() if c in want])

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        if self.down:
            raise requests.ConnectionError("down")
        if url.endswith("/api/streams"):
            lease = json.get("lease")
            for cid in json["ids"]:
                self.snapshots.setdefault(cid, {"contentID": cid, "peers": 0, "seeders": 0, "kbps": 0})
                if lease and self.supports_leases:
                    self.leases.setdefault(cid, set()).add(lease["owner"])
            return resp(200, {"leased": len(json["ids"])} if (lease and self.supports_leases) else {"added": len(json["ids"])})
        if url.endswith("/api/actions"):
            if json["op"] == "release":
                for cid in json["ids"]:
                    self.leases.get(cid, set()).discard(json["owner"])
            elif json["op"] == "remove":
                self.removed.extend(json["ids"])
            return resp(200, {})
        return resp(404)

    def count(self, method, suffix):
        return sum(1 for m, u, *_ in self.calls if m == method and u.endswith(suffix))

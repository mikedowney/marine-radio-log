"""Minimal AIS decoder: only what the vessel-tagging feature needs.

Types 1/2/3 and 18/19 for position, 5 and 24 for name and ship type.
Everything else is ignored. Deliberately small and dependency-free: it
runs beside the watch, so it must never be the reason something falls
over.
"""

import collections, re

SIXBIT = ("@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./"
          "0123456789:;<=>?")
TAG = re.compile(r"^\\([^\\]*)\\(.*)$")
POS_TYPES = (1, 2, 3, 18, 19)


def split_tag(line):
    """('c:123,t:INTERNET*2E', '!AXVDM,...') or (None, line)."""
    m = TAG.match(line)
    return (m.group(1), m.group(2)) if m else (None, line)


def tag_time(tag):
    if not tag:
        return None
    for part in tag.split(","):
        if part.startswith("c:"):
            try:
                return int(part[2:].split("*")[0])
            except ValueError:
                return None
    return None


def tag_source(tag):
    if not tag:
        return None
    for part in tag.split(","):
        if part.startswith("t:"):
            return part[2:].split("*")[0]
    return None


def sixbit(payload):
    bits = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        if v < 0 or v > 63:
            return None
        bits.append(format(v, "06b"))
    return "".join(bits)


def _u(b, i, n):
    return int(b[i:i + n], 2) if len(b) >= i + n else None


def _s(b, i, n):
    if len(b) < i + n:
        return None
    v = int(b[i:i + n], 2)
    return v - (1 << n) if b[i] == "1" else v


def _txt(b, i, n):
    if len(b) < i + n:
        return None
    out = ""
    for k in range(i, i + n - 5, 6):
        out += SIXBIT[int(b[k:k + 6], 2)]
    return out.split("@")[0].strip() or None


class Reassembler:
    """AIS sentences may be split across lines. Keeps partial groups by
    (sequence id, channel) and drops anything stale so a lost fragment
    cannot leak memory."""

    def __init__(self, limit=400):
        self.parts = collections.OrderedDict()
        self.limit = limit

    def feed(self, sentence):
        f = sentence.split(",")
        if len(f) < 7 or f[0][-3:] not in ("VDM", "VDO"):
            return None
        try:
            total, num = int(f[1]), int(f[2])
        except ValueError:
            return None
        talker = f[0][1:3]
        if total == 1:
            return talker, f[5]
        key = (f[3], f[4])
        slot = self.parts.setdefault(key, {})
        slot[num] = f[5]
        self.parts.move_to_end(key)
        while len(self.parts) > self.limit:
            self.parts.popitem(last=False)
        if len(slot) == total:
            self.parts.pop(key, None)
            return talker, "".join(slot[i + 1] for i in range(total))
        return None


def decode(payload):
    """-> dict with mmsi and whatever of name/position was present."""
    b = sixbit(payload)
    if not b or len(b) < 38:
        return None
    t = _u(b, 0, 6)
    out = {"mtype": t, "mmsi": _u(b, 8, 30)}
    if t in (1, 2, 3) and len(b) >= 128:
        out["sog"] = _u(b, 50, 10) / 10.0
        out["lon"] = _s(b, 61, 28) / 600000.0
        out["lat"] = _s(b, 89, 27) / 600000.0
        out["cog"] = _u(b, 116, 12) / 10.0
    elif t == 18 and len(b) >= 124:
        out["sog"] = _u(b, 46, 10) / 10.0
        out["lon"] = _s(b, 57, 28) / 600000.0
        out["lat"] = _s(b, 85, 27) / 600000.0
        out["cog"] = _u(b, 112, 12) / 10.0
    elif t == 19 and len(b) >= 263:
        out["sog"] = _u(b, 46, 10) / 10.0
        out["lon"] = _s(b, 57, 28) / 600000.0
        out["lat"] = _s(b, 85, 27) / 600000.0
        out["cog"] = _u(b, 112, 12) / 10.0
        out["name"] = _txt(b, 143, 120)
    elif t == 5 and len(b) >= 240:
        out["name"] = _txt(b, 112, 120)
        out["ship_type"] = _u(b, 232, 8)
    elif t == 24 and len(b) >= 40:
        if _u(b, 38, 2) == 0:
            out["name"] = _txt(b, 40, 120)
        elif len(b) >= 48:
            out["ship_type"] = _u(b, 40, 8)
    else:
        return out if out.get("mmsi") else None
    # 181/91 are the "not available" sentinels; 0,0 is Null Island.
    if out.get("lat") is not None:
        if (abs(out["lat"]) > 90 or abs(out["lon"]) > 180
                or (out["lat"] == 0 and out["lon"] == 0)):
            out.pop("lat"), out.pop("lon")
    if out.get("sog") is not None and out["sog"] > 102.2:
        out.pop("sog")
    return out

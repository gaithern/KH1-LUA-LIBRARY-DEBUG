"""Slot Map -- live terminal view of KH1's species slots, entity pool and handle buckets.

    python slotmap.py                 # live, redraws until Ctrl-C
    python slotmap.py --interval 0.25 # faster refresh
    python slotmap.py --once          # one frame, then exit
    python slotmap.py --dump <path>   # read a full crash dump instead (needs `pip install minidump`)
    python slotmap.py --log <kh1_native.log>   # mark which models WE loaded
    python slotmap.py --version egs   # override build detection, which is otherwise automatic

Live mode is stdlib-only. It waits for the game if it is not running yet, and keeps waiting if it
exits, so you can leave it open across restarts.

The Steam and EGS builds put every table at a different address, so the build is identified before
anything is read and the matching RVAs are used. Detection re-runs on each attach.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import re
import struct
import sys
import time
from collections import deque

VERSIONS = {
    "steam": {
        "label": "Steam 1.0.0.2",
        "probe": (0x4698D2, 106),
        "image_size": 0x2F91000,
        "slot_table":      0x2869DD0,
        "blob_table":      0xD2ADA0,
        "entity_pool":     0x2D372A0,
        "bucket_table":    0x2EE3980,
        "placement_ptr":   0x296B630,
        "placement_count": 0x296B628,
    },
    "egs": {
        "label": "EGS 1.0.0.10",
        "probe": (0x46A822, 106),
        "image_size": 0x2F92000,
        "slot_table":      0x286A7D0,
        "blob_table":      0xD2B880,
        "entity_pool":     0x2D37CA0,
        "bucket_table":    0x2EE4730,
        "placement_ptr":   0x296C030,
        "placement_count": 0x296C028,
    },
}
PLACEMENT_RECORD_SIZE = 0x78
PLACEMENT_SPECIES_OFF = 0x55
PLACEMENT_RUNLEN_OFF  = 0x56

SLOT_STRIDE, SLOT_COUNT = 0x50, 64
BLOB_STRIDE = 0x40000
ENT_STRIDE, ENT_COUNT = 1200, 96
BUCKET_COUNT = 64
UNCLAIMED = 0xFF
STATE_READY = 6
BLOB_HEADER_FIRST_SECTION = 128
ALLOC_MIN, ALLOC_MAX = 0, 49
RESERVED = {}
for _lo, _n, _who in ((50, 2, "FUN_1401c0550"), (52, 4, "FUN_1401b4230/df90/1920d0"),
                      (52, 8, "FUN_14017da40/180860"), (52, 12, "MgIcon_LoadTextures/18e1d0/2d18f0"),
                      (54, 2, "FUN_140290402"), (56, 4, "FUN_1401b4230"), (60, 4, "FUN_1401a6ad0")):
    for _s in range(_lo, _lo + _n):
        RESERVED.setdefault(_s, _who)

CSI = "\x1b["
RESET = CSI + "0m"


def fg(n):  return f"{CSI}38;5;{n}m"
def bg(n):  return f"{CSI}48;5;{n}m"


DIM, BOLD = CSI + "2m", CSI + "1m"
KIND_STYLE = {
    "primary":      (35,  0, "primary"),
    "member":       (31, 15, "member"),
    "live_unowned": (178, 0, "loaded, unowned"),
    "released":     (240, 250, "released"),
    "free":         (236, 244, "free"),
}
C_WARN, C_BAD, C_OK, C_ACCENT, C_MUTED = fg(178), fg(203), fg(35), fg(80), fg(245)


RUN_CH = "-"


def enable_vt():
    """Turn on ANSI escapes, and switch stdout to UTF-8 -- the default cp1252 console cannot
    encode the run-bar glyph and would raise mid-frame."""
    global RUN_CH
    if os.name == "nt":
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.GetStdHandle(-11)
        mode = wt.DWORD()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        "─".encode(sys.stdout.encoding)
        RUN_CH = "─"
    except Exception:
        RUN_CH = "-"


class LiveReader:
    EXE = "KINGDOM HEARTS FINAL MIX.exe"

    def __init__(self):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi")
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.OpenProcess.restype = wt.HANDLE
        k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k32.ReadProcessMemory.restype = wt.BOOL
        psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                               ctypes.POINTER(wt.DWORD), wt.DWORD]
        psapi.EnumProcessModulesEx.restype = wt.BOOL
        psapi.GetModuleBaseNameA.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_char_p, wt.DWORD]
        psapi.GetModuleBaseNameA.restype = wt.DWORD
        psapi.GetModuleInformation.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
        psapi.GetModuleInformation.restype = wt.BOOL
        self.k32 = k32

        self.pid = self._find_pid()
        if self.pid is None:
            raise RuntimeError("not running")
        self.h = k32.OpenProcess(0x0400 | 0x0010, False, self.pid)
        if not self.h:
            raise RuntimeError(f"OpenProcess failed (err {ctypes.get_last_error()}); try an "
                               "elevated shell")
        self.base, self.image_size = self._module_info(psapi)
        if not self.base:
            raise RuntimeError("module base not found")

    @classmethod
    def _find_pid(cls):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class ENTRY(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]

        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        e = ENTRY(); e.dwSize = ctypes.sizeof(ENTRY)
        found = None
        if k32.Process32First(snap, ctypes.byref(e)):
            while True:
                if e.szExeFile.decode("latin1").lower() == cls.EXE.lower():
                    found = e.th32ProcessID
                    break
                if not k32.Process32Next(snap, ctypes.byref(e)):
                    break
        k32.CloseHandle(snap)
        return found

    def _module_info(self, psapi):
        class MODULEINFO(ctypes.Structure):
            _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", wt.DWORD),
                        ("EntryPoint", ctypes.c_void_p)]

        arr = (ctypes.c_void_p * 1024)()
        needed = wt.DWORD()
        if not psapi.EnumProcessModulesEx(self.h, ctypes.byref(arr), ctypes.sizeof(arr),
                                          ctypes.byref(needed), 3):
            return None, None
        buf = ctypes.create_string_buffer(260)
        for i in range(needed.value // ctypes.sizeof(ctypes.c_void_p)):
            psapi.GetModuleBaseNameA(self.h, arr[i], buf, 260)
            if buf.value.decode("latin1").lower() == self.EXE.lower():
                mi = MODULEINFO()
                size = (mi.SizeOfImage if psapi.GetModuleInformation(
                    self.h, arr[i], ctypes.byref(mi), ctypes.sizeof(mi)) else None)
                return arr[i], size
        return None, None

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = self.k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf,
                                        ctypes.c_size_t(size), ctypes.byref(got))
        return buf.raw[:got.value] if ok and got.value == size else None

    def alive(self):
        return self.read(self.base, 2) is not None

    @property
    def source(self):
        return f"live pid {self.pid}"


class DumpReader:
    def __init__(self, path):
        from minidump.minidumpfile import MinidumpFile
        self.mf = MinidumpFile.parse(path)
        self.rdr = self.mf.get_reader()
        self.path = path
        self.base = None
        self.image_size = None
        for m in self.mf.modules.modules:
            n = m.name.split("\\")[-1]
            if "FINAL MIX" in n.upper() and n.lower().endswith(".exe"):
                self.base = m.baseaddress
                self.image_size = getattr(m, "size", None)
                break
        if self.base is None:
            raise RuntimeError("exe module not found in dump")

    def read(self, addr, size):
        try:
            return self.rdr.read(addr, size)
        except Exception:
            return None

    def alive(self):
        return True

    @property
    def source(self):
        return os.path.basename(self.path)


def attach_version(reader, forced=None):
    """Work out which build this is and hang its RVA table on the reader.

    Guessing wrong here does not fail loudly -- every address still resolves to *something*, so the
    map would render confidently from the wrong memory. So it refuses rather than defaults."""
    if forced:
        reader.version = forced
        reader.version_how = "--version"
    else:
        hits = []
        for key, v in VERSIONS.items():
            rva, want = v["probe"]
            b = reader.read(reader.base + rva, 1)
            if b and b[0] == want:
                hits.append(key)
        if len(hits) == 1:
            reader.version, reader.version_how = hits[0], "probe byte"
        else:
            size = getattr(reader, "image_size", None)
            by_size = [k for k, v in VERSIONS.items() if size and v["image_size"] == size]
            if len(by_size) == 1:
                reader.version = by_size[0]
                reader.version_how = "image size 0x%X" % size
            else:
                raise RuntimeError(
                    "cannot identify the game build (probe matched %s, image size %s) -- "
                    "pass --version %s" % (hits or "nothing",
                                           ("0x%X" % size) if size else "unknown",
                                           "|".join(VERSIONS)))
    reader.rva = VERSIONS[reader.version]
    return reader


def snapshot(reader, ours):
    """A slot can be LOADED yet carry no owner stamp. Release wipes the blob but leaves state and
    the cached name behind, so a valid blob header is what separates live from released."""
    base, rva = reader.base, reader.rva
    raw = reader.read(base + rva["slot_table"], SLOT_STRIDE * SLOT_COUNT)
    if raw is None:
        return None
    slots = []
    for i in range(SLOT_COUNT):
        rec = raw[i * SLOT_STRIDE:(i + 1) * SLOT_STRIDE]
        owner, runlen, state = rec[0], rec[1], rec[3]
        name = rec[4:0x24].split(b"\x00")[0].decode("latin1", "replace")
        hdr = reader.read(base + rva["blob_table"] + i * BLOB_STRIDE + 4, 4)
        sec4 = struct.unpack("<i", hdr)[0] if hdr else 0
        live = state == STATE_READY and bool(name) and sec4 == BLOB_HEADER_FIRST_SECTION
        if owner == UNCLAIMED:
            kind = "live_unowned" if live else ("released" if (state == STATE_READY and name)
                                               else "free")
        else:
            kind = "primary" if owner == i else "member"
        dead = (owner != UNCLAIMED and state == STATE_READY and bool(name)
                and sec4 != BLOB_HEADER_FIRST_SECTION and owner == i)
        slots.append({"i": i, "owner": owner, "runLen": runlen, "state": state, "name": name,
                      "sec4": sec4, "kind": kind, "deadblob": dead,
                      "ours": name.replace(".mdls", "") in ours,
                      "run_start": None, "run_name": None})

    starts = [s["i"] for s in slots if s["kind"] in ("primary", "live_unowned")]
    occ = [False] * SLOT_COUNT
    runs, cover = [], [0] * SLOT_COUNT
    for n, i in enumerate(starts):
        s = slots[i]
        nxt = starts[n + 1] if n + 1 < len(starts) else SLOT_COUNT
        end = min(i + max(s["runLen"], 1), nxt, SLOT_COUNT)
        runs.append({"start": i, "len": end - i, "name": s["name"],
                     "kind": s["kind"], "ours": s["ours"]})
        for j in range(i, end):
            occ[j] = True
            cover[j] += 1
            slots[j]["ours"] = s["ours"]
            if j != i:
                slots[j]["run_start"] = i
                slots[j]["run_name"] = s["name"]
    for s in slots:
        if s["owner"] != UNCLAIMED:
            occ[s["i"]] = True
        if cover[s["i"]] and s["i"] not in starts:
            s["kind"] = "member"
            s["deadblob"] = False
        s["occupied"] = occ[s["i"]]
        s["overlap"] = cover[s["i"]] > 1

    ents = []
    pool = reader.read(base + rva["entity_pool"], ENT_STRIDE * ENT_COUNT)
    for i in range(ENT_COUNT):
        if pool is None:
            ents.append({"i": i, "id": 0, "cat": 0, "live": False}); continue
        rec = pool[i * ENT_STRIDE:(i + 1) * ENT_STRIDE]
        eid = struct.unpack_from("<I", rec, 4)[0]
        f374 = struct.unpack_from("<I", rec, 0x374)[0]
        ents.append({"i": i, "id": eid, "cat": (eid >> 16) & 0xFFFF,
                     "live": (f374 & 3) == 1 and eid not in (0, 0xFFFFFFFF)})

    braw = reader.read(base + rva["bucket_table"], 8 * BUCKET_COUNT)
    buckets = []
    bt = []
    for i in range(BUCKET_COUNT):
        v = struct.unpack_from("<Q", braw, i * 8)[0] if braw else 0xFFFFFFFFFFFFFFFF
        bt.append(v)
        buckets.append(v != 0xFFFFFFFFFFFFFFFF)

    blob_base = base + rva["blob_table"]
    def _resolve_slot(h):
        if h == 0:
            return None
        b, off = (h & 0x7FFFFFFF) >> 25, h & 0x1FFFFFF
        if b >= BUCKET_COUNT or bt[b] == 0xFFFFFFFFFFFFFFFF:
            return None
        p = bt[b] | off
        return (p - blob_base) // BLOB_STRIDE if blob_base <= p < blob_base + 64 * BLOB_STRIDE else None
    referenced = {}
    if pool is not None:
        for e in ents:
            if not e["live"]:
                continue
            rec = pool[e["i"] * ENT_STRIDE:(e["i"] + 1) * ENT_STRIDE]
            for off in (0x68, 0x134, 0x138, 0x13c, 0x154, 0x1d0, 0x1d4):
                s = _resolve_slot(struct.unpack_from("<I", rec, off)[0])
                if s is not None:
                    referenced.setdefault(s, []).append(e["i"])
    roster = [False] * SLOT_COUNT
    roster_runs = []
    tp = reader.read(base + rva["placement_ptr"], 8)
    tc = reader.read(base + rva["placement_count"], 4)
    if tp and tc:
        tbl = struct.unpack("<Q", tp)[0]
        cnt = struct.unpack("<i", tc)[0]
        if tbl and 0 < cnt <= 4096:
            recs = reader.read(tbl, cnt * PLACEMENT_RECORD_SIZE)
            if recs:
                seen = {}
                for i in range(cnt):
                    rec = recs[i * PLACEMENT_RECORD_SIZE:(i + 1) * PLACEMENT_RECORD_SIZE]
                    sp = rec[PLACEMENT_SPECIES_OFF]
                    ln = struct.unpack("<b", rec[PLACEMENT_RUNLEN_OFF:PLACEMENT_RUNLEN_OFF + 1])[0]
                    if ln < 1:
                        ln = 1
                    if sp >= SLOT_COUNT:
                        continue
                    seen[sp] = max(seen.get(sp, 0), ln)
                for sp, ln in seen.items():
                    roster_runs.append((sp, ln))
                    for k in range(ln):
                        if sp + k < SLOT_COUNT:
                            roster[sp + k] = True

    for s in slots:
        s["roster"] = roster[s["i"]]
        s["refs"] = referenced.get(s["i"], [])
        s["at_risk"] = bool(s["refs"]) and (s["overlap"] or s["deadblob"]
                                            or s["kind"] == "released")
        s["stale"] = (s["kind"] in ("primary", "live_unowned")
                      and not s["roster"] and not s["refs"])

    for s in slots:
        if s["run_start"] is not None:
            s["stale"] = (slots[s["run_start"]]["stale"]
                          and not s["roster"] and not s["refs"])

    for s in slots:
        s["available"] = (not s["roster"] and not s["refs"]
                          and (s["stale"] or s["kind"] in ("free", "released")))

    return {"base": base, "slots": slots, "runs": runs, "entities": ents, "buckets": buckets,
            "roster": roster, "roster_runs": sorted(roster_runs)}


def parse_log(path, target_path=None):
    """Model stems this log shows US loading, plus a warning if it cannot actually cover the target.

    kh1_native.log rotates. When it does, loads from an older session are gone and EVERY block in an
    older dump silently reads as 'not ours' -- which once nearly produced the conclusion that native
    rosters occupy the engine scratch range. Better to say the attribution is unusable."""
    if not path:
        return set(), None
    if not os.path.exists(path):
        return set(), "log not found: %s -- no '*' attribution" % path
    pat = re.compile(r"recorded triggered load .*model=([A-Za-z0-9_]+)\.mdls")
    stamp = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]")
    ours, first, last = set(), None, None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = stamp.match(line)
            if m:
                t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                if first is None:
                    first = t
                last = t
            ours.update(pat.findall(line))
    if first is None:
        return ours, "log has no timestamped lines -- cannot check coverage"

    log_day = time.localtime(os.path.getmtime(path))
    def hhmmss(s):
        return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)
    span = "%s-%s on %04d-%02d-%02d" % (hhmmss(first), hhmmss(last),
                                        log_day.tm_year, log_day.tm_mon, log_day.tm_mday)
    if target_path and os.path.exists(target_path):
        d = time.localtime(os.path.getmtime(target_path))
        dt = d.tm_hour * 3600 + d.tm_min * 60 + d.tm_sec
        same_day = (d.tm_year, d.tm_mon, d.tm_mday) == (log_day.tm_year, log_day.tm_mon, log_day.tm_mday)
        if not same_day:
            return ours, ("log covers %s but this dump is from %04d-%02d-%02d -- it has ROTATED past "
                          "this dump, so '*' attribution is WRONG (everything reads as not-ours)"
                          % (span, d.tm_year, d.tm_mon, d.tm_mday))
        if not (first <= dt <= last + 120):
            return ours, ("log covers %s but this dump is from %s -- outside that span, so '*' "
                          "attribution is unreliable" % (span, hhmmss(dt)))
    return ours, None


def slot_strip(slots):
    ruler_t = "".join(str(i // 10) if i % 10 == 0 else " " for i in range(SLOT_COUNT))
    ruler_u = "".join(str(i % 10) for i in range(SLOT_COUNT))
    cells = []
    for s in slots:
        b, f, _ = KIND_STYLE[s["kind"]]
        glyph = ("!" if s["overlap"] else "~" if s["stale"] else "_" if s["ours"]
                 else "R" if s["i"] in RESERVED else " ")
        col = fg(203) + BOLD if s["overlap"] else fg(f)
        cells.append(bg(b) + col + glyph + RESET)
    return [C_MUTED + ruler_t + RESET, C_MUTED + ruler_u + RESET, "".join(cells)]


def run_rows(runs):
    """Pack runs onto rows so overlapping spans are forced apart and read as a collision."""
    rows = []
    for r in sorted(runs, key=lambda r: r["start"]):
        placed = False
        for row in rows:
            if all(r["start"] >= x["start"] + x["len"] or r["start"] + r["len"] <= x["start"]
                   for x in row):
                row.append(r); placed = True; break
        if not placed:
            rows.append([r])
    clash = set()
    for a in runs:
        for b in runs:
            if a is not b and a["start"] < b["start"] + b["len"] and b["start"] < a["start"] + a["len"]:
                clash.add(id(a)); clash.add(id(b))
    out = []
    for row in rows:
        line = [" "] * SLOT_COUNT
        colored = ""
        cur = 0
        for r in sorted(row, key=lambda r: r["start"]):
            colored += " " * (r["start"] - cur)
            col = fg(203) if id(r) in clash else (fg(178) if r["kind"] == "live_unowned" else fg(35))
            colored += col + RUN_CH * r["len"] + RESET
            cur = r["start"] + r["len"]
        colored += " " * (SLOT_COUNT - cur)
        names = "  ".join(
            f"{fg(203) if id(r) in clash else C_MUTED}{r['start']}-{r['start']+r['len']-1}"
            f"{' *' if r['ours'] else ''} {r['name'] or '?'}{RESET}"
            for r in sorted(row, key=lambda r: r["start"]))
        out.append(colored + "  " + names)
    return out


def entity_grid(ents):
    lines = []
    for row in range(0, ENT_COUNT, 32):
        cells = ""
        for e in ents[row:row + 32]:
            if not e["live"]:
                cells += bg(236) + " " + RESET
            else:
                c = {3: 80, 4: 31, 5: 35}.get(e["cat"], 178)
                cells += bg(c) + " " + RESET
        lines.append(cells)
    return lines


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def render(st, events, source, frame, warning=None):
    L = []
    slots = st["slots"]
    free = sum(1 for s in slots if s["available"] and ALLOC_MIN <= s["i"] <= ALLOC_MAX)
    unowned = sum(1 for s in slots if s["kind"] == "live_unowned")
    overlaps = sum(1 for s in slots if s["overlap"])
    live_e = sum(1 for e in st["entities"] if e["live"])
    buckets = sum(1 for b in st["buckets"] if b)

    L.append(f"{BOLD}KH1 Slot Map{RESET}  {C_MUTED}{source}   base 0x{st['base']:X}   "
             f"{time.strftime('%H:%M:%S')}   frame {frame}{RESET}")
    def stat(label, val, warn=False, bad=False):
        c = C_BAD if bad else (C_WARN if warn else C_OK)
        return f"{C_MUTED}{label} {RESET}{c}{val}{RESET}"
    L.append("  ".join([
        stat("allocatable", f"{free}/{ALLOC_MAX - ALLOC_MIN + 1}", warn=free < 6),
        stat("loaded-unowned", unowned, warn=unowned > 0),
        stat("overlaps", overlaps),
        stat("dead blobs", sum(1 for s in slots if s["deadblob"])),
        stat("AT RISK", sum(1 for s in slots if s["at_risk"]),
             bad=any(s["at_risk"] for s in slots)),
        stat("roster", "%d slots (%d room, %d ours)" % (
            sum(1 for s in slots if s["roster"]),
            sum(1 for s in slots if s["roster"] and not s["ours"]),
            sum(1 for s in slots if s["roster"] and s["ours"]))),
        stat("stale (reclaimable)", sum(1 for s in slots if s["stale"])),
        stat("entities", f"{live_e}/{ENT_COUNT}"),
        stat("buckets", f"{buckets}/{BUCKET_COUNT}", warn=buckets > 56),
    ]))
    if warning:
        L.append(f"{fg(203)}{BOLD}! {warning}{RESET}")
    L.append("")
    L.append(f"{C_MUTED}SPECIES SLOTS{RESET}   " + "  ".join(
        bg(b) + "  " + RESET + " " + C_MUTED + lbl + RESET
        for b, _, lbl in [KIND_STYLE[k] for k in
                          ("primary", "member", "live_unowned", "released", "free")])
        + f"   {fg(203)}!{RESET}{C_MUTED} overlap   {C_OK}~{C_MUTED} reclaimable   _ ours   "
        + f"R contended (engine scratch; rooms use it too){RESET}")
    L += slot_strip(slots)
    L.append("".join((C_ACCENT + "+" if s["ours"] else C_ACCENT + "#") if s["roster"]
                     else C_MUTED + "." for s in slots) + RESET
             + f"  {C_MUTED}placement roster ({len(st['roster_runs'])} entries)"
             + f"   # room   + ours (injected){RESET}")
    if st["roster_runs"]:
        L.append("   " + C_MUTED + "  ".join("%d-%d" % (sp, sp + ln - 1)
                                             for sp, ln in st["roster_runs"]) + RESET)
    L.append("")
    rr = run_rows(st["runs"])
    if rr:
        L.append(f"{C_MUTED}RUNS{RESET}")
        L += rr
    L.append("")
    L.append(f"{C_MUTED}OCCUPIED SLOTS{RESET}")
    L.append(f"{C_MUTED}{'slot':>4} {'kind':<15} {'own':>4} {'run':>4} {'st':>3} "
             f"{'blob+4':>11}  model{RESET}")
    for s in slots:
        if s["kind"] == "free":
            continue
        if s["kind"] == "member" and not s["name"] and not s["overlap"]:
            continue
        b, _, lbl = KIND_STYLE[s["kind"]]
        mark = f"{fg(203)} OVERLAP{RESET}" if s["overlap"] else ""
        if s["deadblob"]:
            mark += f"{fg(203)} DEAD-BLOB{RESET}"
        if s["ours"] and s["i"] in RESERVED:
            mark += f"{C_MUTED} in-engine-scratch({RESERVED[s['i']]}){RESET}"
        if s["refs"]:
            col = fg(203) + BOLD if s["at_risk"] else C_MUTED
            mark += f"{col} live-entities{s['refs']}{RESET}"
        if s["at_risk"]:
            mark += f"{fg(203)}{BOLD} AT RISK{RESET}"
        if s["roster"] and s["ours"]:
            mark += f"{C_ACCENT} in-roster (OURS, injected){RESET}"
        elif s["roster"]:
            mark += f"{C_ACCENT} in-roster{RESET}"
        elif s["stale"]:
            mark += f"{C_OK} STALE (reclaimable){RESET}"
        ours = f"{C_ACCENT} *{RESET}" if s["ours"] else ""
        col = C_WARN if s["kind"] == "live_unowned" else ""
        holds = s["run_name"] or s["name"] or "-"
        L.append(f"{s['i']:>4} {col}{lbl:<15}{RESET} 0x{s['owner']:02X} {s['runLen']:>4} "
                 f"{s['state']:>3} {s['sec4']:>11}  {holds}{ours}{mark}")
    L.append("")
    L.append(f"{C_MUTED}ENTITY POOL{RESET}  {bg(80)}  {RESET}{C_MUTED} cat3 {RESET}"
             f"{bg(31)}  {RESET}{C_MUTED} cat4 {RESET}{bg(35)}  {RESET}{C_MUTED} cat5 {RESET}"
             f"{bg(178)}  {RESET}{C_MUTED} other{RESET}")
    L += entity_grid(st["entities"])
    if events:
        L.append("")
        L.append(f"{C_MUTED}CHANGES{RESET}")
        L += list(events)
    return L


def diff_events(prev, cur, events):
    """Only changes are worth watching live -- a spawn claiming a run shows up here instantly."""
    ts = time.strftime("%H:%M:%S")
    for a, b in zip(prev["slots"], cur["slots"]):
        if a["kind"] != b["kind"] or a["name"] != b["name"]:
            col = C_BAD if b["overlap"] else (C_WARN if b["kind"] == "live_unowned" else C_OK)
            events.append(f"{C_MUTED}{ts}{RESET} slot {b['i']:>2}  "
                          f"{C_MUTED}{KIND_STYLE[a['kind']][2]}{RESET} -> {col}"
                          f"{KIND_STYLE[b['kind']][2]}{RESET}  {b['name'] or a['name'] or ''}")
    po = sum(1 for s in prev["slots"] if s["overlap"])
    co = sum(1 for s in cur["slots"] if s["overlap"])
    if co > po:
        events.append(f"{C_MUTED}{ts}{RESET} {C_BAD}OVERLAP appeared "
                      f"({po} -> {co} slots){RESET}")


def main():
    ap = argparse.ArgumentParser(description="Live view of KH1 slot/entity state.")
    ap.add_argument("--dump", help="read a full crash dump instead of the live process")
    ap.add_argument("--log", help="kh1_native.log, to mark which models we loaded")
    ap.add_argument("--interval", type=float, default=0.5, help="seconds between redraws")
    ap.add_argument("--once", action="store_true", help="draw one frame and exit")
    ap.add_argument("--version", choices=sorted(VERSIONS),
                    help="override build detection (normally automatic)")
    args = ap.parse_args()

    enable_vt()
    ours, log_warning = parse_log(args.log, args.dump)
    frame, prev, events = 0, None, deque(maxlen=12)
    reader = attach_version(DumpReader(args.dump), args.version) if args.dump else None

    sys.stdout.write(CSI + "?25l")
    try:
        while True:
            if reader is None or not reader.alive():
                if args.dump:
                    break
                try:
                    reader = attach_version(LiveReader(), args.version)
                    events.append(f"{C_MUTED}{time.strftime('%H:%M:%S')}{RESET} "
                                  f"{C_OK}attached to pid {reader.pid}{RESET} {C_MUTED}"
                                  f"({VERSIONS[reader.version]['label']}){RESET}")
                    prev = None
                except RuntimeError as exc:
                    sys.stdout.write(CSI + "H" + CSI + "2J")
                    sys.stdout.write(f"{BOLD}KH1 Slot Map{RESET}\n{C_MUTED}waiting for the game "
                                     f"({exc})...{RESET}\n")
                    sys.stdout.flush()
                    if args.once:
                        return 1
                    time.sleep(1.0)
                    continue

            st = snapshot(reader, ours)
            if st is None:
                reader = None
                continue
            frame += 1
            if prev is not None:
                diff_events(prev, st, events)
            prev = st

            lines = render(st, events,
                           "%s   %s via %s" % (reader.source, VERSIONS[reader.version]["label"],
                                               reader.version_how),
                           frame, log_warning)
            out = CSI + "H"
            for ln in lines:
                out += ln + CSI + "K\n"
            out += CSI + "J"
            sys.stdout.write(out)
            sys.stdout.flush()

            if args.once or args.dump:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(CSI + "?25h" + RESET + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

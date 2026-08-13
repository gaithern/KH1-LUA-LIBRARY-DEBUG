"""Slot Map -- live terminal view of KH1's species slots, entity pool and handle buckets.

    python slotmap.py                 # live, redraws until Ctrl-C
    python slotmap.py --interval 0.25 # faster refresh
    python slotmap.py --once          # one frame, then exit
    python slotmap.py --dump <path>   # read a full crash dump instead (needs `pip install minidump`)
    python slotmap.py --log <kh1_native.log>   # mark which models WE loaded

Live mode is stdlib-only. It waits for the game if it is not running yet, and keeps waiting if it
exits, so you can leave it open across restarts.
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

# --- Steam 1.0.0.2 RVAs. EGS differs. ---
RVA_SLOT_TABLE   = 0x2869DD0    # 64 x 0x50   owner/runLen/flags/state/name
RVA_BLOB_TABLE   = 0xD2ADA0     # 64 x 0x40000 resource blobs
RVA_ENTITY_POOL  = 0x2D372A0    # 96 x 1200
RVA_BUCKET_TABLE = 0x2EE3980    # 64 x 8
RVA_PLACEMENT_PTR   = 0x296B630 # -> the room's placement table
RVA_PLACEMENT_COUNT = 0x296B628
PLACEMENT_RECORD_SIZE = 0x78
PLACEMENT_SPECIES_OFF = 0x55    # which species slot this record's creature loads into
PLACEMENT_RUNLEN_OFF  = 0x56    # how many consecutive slots it claims

SLOT_STRIDE, SLOT_COUNT = 0x50, 64
BLOB_STRIDE = 0x40000
ENT_STRIDE, ENT_COUNT = 1200, 96
BUCKET_COUNT = 64
UNCLAIMED = 0xFF
STATE_READY = 6
BLOB_HEADER_FIRST_SECTION = 128
ALLOC_MIN, ALLOC_MAX = 0, 49         # no floor: the party roster varies, so occupancy is the real
                                     # guard. 50-63 is engine scratch, force-grabbed MID-ROOM.
# Slots other engine subsystems grab via fnc_release_species_slot_run(species, runLen), which
# returns the blob base -- they call it purely to claim that buffer. From all 18 call sites.
# 50-63 is CONTENDED, not reserved: engine subsystems grab it as scratch (MgIcon_LoadTextures reads
# command2/uitex.bin into slot 52), but room rosters also place creatures there -- live-confirmed in
# a session with zero spawns of ours, whose authored roster ran xa_ex_2050 to 46-50 and xa_ex_2181
# at 51. The engine evidently accepts that risk. We avoid it because we have direct evidence of slot
# 56 being repeatedly overwritten, but it is a safety margin, not a boundary the engine respects.
#
# NOT listed: slots 16/17. FUN_140179000 grabs them ONLY when called with a non-zero argument; with
# zero (evidently the normal case) it uses static buffers at 0x14232D570 and never touches the slot
# table. The party occupies 10-16 and 17-23 in every dump observed, so flagging them was wrong.
RESERVED = {}
for _lo, _n, _who in ((50, 2, "FUN_1401c0550"), (52, 4, "FUN_1401b4230/df90/1920d0"),
                      (52, 8, "FUN_14017da40/180860"), (52, 12, "MgIcon_LoadTextures/18e1d0/2d18f0"),
                      (54, 2, "FUN_140290402"), (56, 4, "FUN_1401b4230"), (60, 4, "FUN_1401a6ad0")):
    for _s in range(_lo, _lo + _n):
        RESERVED.setdefault(_s, _who)

# --- ANSI ---
CSI = "\x1b["
RESET = CSI + "0m"


def fg(n):  return f"{CSI}38;5;{n}m"
def bg(n):  return f"{CSI}48;5;{n}m"


DIM, BOLD = CSI + "2m", CSI + "1m"
# kind -> (background colour, foreground for the slot glyph, short label)
KIND_STYLE = {
    "primary":      (35,  0, "primary"),
    "member":       (31, 15, "member"),
    "live_unowned": (178, 0, "loaded, unowned"),
    "released":     (240, 250, "released"),
    "free":         (236, 244, "free"),
}
C_WARN, C_BAD, C_OK, C_ACCENT, C_MUTED = fg(178), fg(203), fg(35), fg(80), fg(245)


RUN_CH = "-"   # replaced with a box-drawing rule if the console can encode it


def enable_vt():
    """Turn on ANSI escapes, and switch stdout to UTF-8 -- the default cp1252 console cannot
    encode the run-bar glyph and would raise mid-frame."""
    global RUN_CH
    if os.name == "nt":
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.GetStdHandle(-11)
        mode = wt.DWORD()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        "─".encode(sys.stdout.encoding)
        RUN_CH = "─"
    except Exception:
        RUN_CH = "-"


# --------------------------------------------------------------------------- readers
class LiveReader:
    EXE = "KINGDOM HEARTS FINAL MIX.exe"

    def __init__(self):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi")
        # Handles and addresses are 64-bit; without argtypes ctypes truncates them to int32.
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
        self.k32 = k32

        self.pid = self._find_pid()
        if self.pid is None:
            raise RuntimeError("not running")
        self.h = k32.OpenProcess(0x0400 | 0x0010, False, self.pid)  # QUERY_INFORMATION | VM_READ
        if not self.h:
            raise RuntimeError(f"OpenProcess failed (err {ctypes.get_last_error()}); try an "
                               "elevated shell")
        self.base = self._module_base(psapi)
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

    def _module_base(self, psapi):
        arr = (ctypes.c_void_p * 1024)()
        needed = wt.DWORD()
        if not psapi.EnumProcessModulesEx(self.h, ctypes.byref(arr), ctypes.sizeof(arr),
                                          ctypes.byref(needed), 3):  # LIST_MODULES_ALL
            return None
        buf = ctypes.create_string_buffer(260)
        for i in range(needed.value // ctypes.sizeof(ctypes.c_void_p)):
            psapi.GetModuleBaseNameA(self.h, arr[i], buf, 260)
            if buf.value.decode("latin1").lower() == self.EXE.lower():
                return arr[i]
        return None

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
        for m in self.mf.modules.modules:
            n = m.name.split("\\")[-1]
            if "FINAL MIX" in n.upper() and n.lower().endswith(".exe"):
                self.base = m.baseaddress
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


# --------------------------------------------------------------------------- model
def snapshot(reader, ours):
    """A slot can be LOADED yet carry no owner stamp. Release wipes the blob but leaves state and
    the cached name behind, so a valid blob header is what separates live from released."""
    base = reader.base
    raw = reader.read(base + RVA_SLOT_TABLE, SLOT_STRIDE * SLOT_COUNT)
    if raw is None:
        return None
    slots = []
    for i in range(SLOT_COUNT):
        rec = raw[i * SLOT_STRIDE:(i + 1) * SLOT_STRIDE]
        owner, runlen, state = rec[0], rec[1], rec[3]
        name = rec[4:0x24].split(b"\x00")[0].decode("latin1", "replace")
        hdr = reader.read(base + RVA_BLOB_TABLE + i * BLOB_STRIDE + 4, 4)
        sec4 = struct.unpack("<i", hdr)[0] if hdr else 0
        live = state == STATE_READY and bool(name) and sec4 == BLOB_HEADER_FIRST_SECTION
        if owner == UNCLAIMED:
            kind = "live_unowned" if live else ("released" if (state == STATE_READY and name)
                                               else "free")
        else:
            kind = "primary" if owner == i else "member"
        # Owned + ready + named, but no valid blob header: the slot claims to hold a loaded species
        # whose data is gone. Anything still referencing it reads wiped memory.
        dead = (owner != UNCLAIMED and state == STATE_READY and bool(name)
                and sec4 != BLOB_HEADER_FIRST_SECTION and owner == i)
        slots.append({"i": i, "owner": owner, "runLen": runlen, "state": state, "name": name,
                      "sec4": sec4, "kind": kind, "deadblob": dead,
                      "ours": name.replace(".mdls", "") in ours,
                      "run_start": None, "run_name": None})

    # A slot with a VALID header is a run START: another creature's blob physically cannot span it,
    # because slot N's base would then be mid-blob and read arbitrary bytes rather than 128. So a
    # runLen that appears to reach past a start is STALE, and truncating there removes the phantom
    # overlaps that reading runLen literally produces.
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
            # A slot's ownership is its RUN START's, never its own stale label -- overwrite rather
            # than OR, or a member keeps "ours" from a creature evicted rooms ago.
            slots[j]["ours"] = s["ours"]
            if j != i:
                slots[j]["run_start"] = i
                slots[j]["run_name"] = s["name"]
    for s in slots:
        if s["owner"] != UNCLAIMED:
            occ[s["i"]] = True
        # Covered by someone else's run: it is a member, whatever stale state/name it still carries.
        if cover[s["i"]] and s["i"] not in starts:
            s["kind"] = "member"
            s["deadblob"] = False
        s["occupied"] = occ[s["i"]]
        s["overlap"] = cover[s["i"]] > 1

    ents = []
    pool = reader.read(base + RVA_ENTITY_POOL, ENT_STRIDE * ENT_COUNT)
    for i in range(ENT_COUNT):
        if pool is None:
            ents.append({"i": i, "id": 0, "cat": 0, "live": False}); continue
        rec = pool[i * ENT_STRIDE:(i + 1) * ENT_STRIDE]
        eid = struct.unpack_from("<I", rec, 4)[0]
        f374 = struct.unpack_from("<I", rec, 0x374)[0]
        ents.append({"i": i, "id": eid, "cat": (eid >> 16) & 0xFFFF,
                     "live": (f374 & 3) == 1 and eid not in (0, 0xFFFFFFFF)})

    braw = reader.read(base + RVA_BUCKET_TABLE, 8 * BUCKET_COUNT)
    buckets = []
    bt = []
    for i in range(BUCKET_COUNT):
        v = struct.unpack_from("<Q", braw, i * 8)[0] if braw else 0xFFFFFFFFFFFFFFFF
        bt.append(v)
        buckets.append(v != 0xFFFFFFFFFFFFFFFF)

    # Which blob slots a LIVE entity actually points into. Stale slot metadata being overwritten is
    # NORMAL -- the engine never cleans up, it just overwrites what it needs. It only matters when
    # something still references it, so that is the only thing worth alarming about.
    blob_base = base + RVA_BLOB_TABLE
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
    # The room's AUTHORED roster: every creature this room can place, with the species slot it loads
    # into. This is what says which blocks belong to the room we are standing in -- anything loaded
    # and outside it is left over from a room we have left, because the engine never clears ownership.
    roster = [False] * SLOT_COUNT
    roster_runs = []
    tp = reader.read(base + RVA_PLACEMENT_PTR, 8)
    tc = reader.read(base + RVA_PLACEMENT_COUNT, 4)
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
                    if sp >= SLOT_COUNT:     # 255 is a "none" marker in some records
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
        # Dangerous only if a live entity depends on it AND its data is not intact.
        s["at_risk"] = bool(s["refs"]) and (s["overlap"] or s["deadblob"]
                                            or s["kind"] == "released")
        # Loaded, but this room neither lists it nor points at it: a leftover we can reuse.
        s["stale"] = (s["kind"] in ("primary", "live_unowned")
                      and not s["roster"] and not s["refs"])

    # A reclaimable run's MEMBERS are reclaimable too -- counting only starts reported 3 slots free
    # when 8 were. A member still needs its own roster/reference check: another roster entry can
    # legitimately cover part of a run whose start is stale.
    for s in slots:
        if s["run_start"] is not None:
            s["stale"] = (slots[s["run_start"]]["stale"]
                          and not s["roster"] and not s["refs"])

    # Match the DLL's ComputeSlotAvailability exactly: a slot is usable unless this room's roster
    # claims it or a live entity reads it. Stale ownership counts for nothing -- an owner-based
    # count here reported "0 allocatable" while 8 slots were reclaimable.
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


# --------------------------------------------------------------------------- render
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
        colored += " " * (SLOT_COUNT - cur)          # pad to the strip width, ANSI-free
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
        # Overlaps/dead blobs on unreferenced slots are normal engine churn, not a problem.
        stat("overlaps", overlaps),
        stat("dead blobs", sum(1 for s in slots if s["deadblob"])),
        stat("AT RISK", sum(1 for s in slots if s["at_risk"]),
             bad=any(s["at_risk"] for s in slots)),
        # We splice our own records into the room's placement table, so they show up in the roster
        # exactly like authored ones. Split them out -- only the room's are really the room's.
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
    # The room's authored roster, straight from its placement table. Anything loaded OUTSIDE this is
    # a leftover from a room we have left, and is ours to reuse.
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
        # Run starts and anything anomalous. Nameless members are just span filler.
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
        # Show what the slot ACTUALLY holds: a run member belongs to its run's start, whose cached
        # name is current because it really is that asset's primary.
        #
        # A member's OWN name field is never displayed. It lives in the slot-table entry (outside the
        # blob), is only written while that slot is itself a run start, and is never cleared -- so on
        # a member it names a creature evicted rooms ago. It carries no information about what is
        # resident and read as a live occupant twice, so it is dropped rather than annotated.
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
    args = ap.parse_args()

    enable_vt()
    ours, log_warning = parse_log(args.log, args.dump)
    frame, prev, events = 0, None, deque(maxlen=12)
    reader = DumpReader(args.dump) if args.dump else None

    sys.stdout.write(CSI + "?25l")   # hide cursor
    try:
        while True:
            if reader is None or not reader.alive():
                if args.dump:
                    break
                try:
                    reader = LiveReader()
                    events.append(f"{C_MUTED}{time.strftime('%H:%M:%S')}{RESET} "
                                  f"{C_OK}attached to pid {reader.pid}{RESET}")
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

            lines = render(st, events, reader.source, frame, log_warning)
            out = CSI + "H"                       # home, then clear each line as we go
            for ln in lines:
                out += ln + CSI + "K\n"
            out += CSI + "J"                      # clear anything below
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

"""Slot Map -- renders KH1's species-slot table, entity pool and resource-handle buckets as HTML.

Reads either the live game process or a full crash dump, and writes a self-contained page.

    python slotmap.py                          # live game -> slotmap.html
    python slotmap.py --dump C:\\KH1CrashDumps\\kh1_full_161945_23236.dmp
    python slotmap.py --log  <path to kh1_native.log>   # mark which species WE loaded
    python slotmap.py -o somewhere.html

Live mode needs nothing but the stdlib. Dump mode needs `pip install minidump`.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import struct
import sys

# --- Steam 1.0.0.2 RVAs. EGS differs; pass --base-rva overrides if this is ever ported. ---
RVA_SLOT_TABLE   = 0x2869DD0    # 64 x 0x50   owner/runLen/flags/state/name
RVA_BLOB_TABLE   = 0xD2ADA0     # 64 x 0x40000 resource blobs
RVA_ENTITY_POOL  = 0x2D372A0    # 96 x 1200
RVA_BUCKET_TABLE = 0x2EE3980    # 64 x 8

SLOT_STRIDE, SLOT_COUNT = 0x50, 64
BLOB_STRIDE = 0x40000
ENT_STRIDE, ENT_COUNT = 1200, 96
BUCKET_COUNT = 64
UNCLAIMED = 0xFF
STATE_READY = 6
BLOB_HEADER_FIRST_SECTION = 128


# --------------------------------------------------------------------------- readers
class LiveReader:
    """Reads the running game via ReadProcessMemory."""

    def __init__(self, exe_name="KINGDOM HEARTS FINAL MIX.exe"):
        k32, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi")
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
        pid = self._find_pid(exe_name)
        if pid is None:
            raise RuntimeError(f"{exe_name} is not running")
        self.pid = pid
        # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        self.h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not self.h:
            raise RuntimeError(
                f"OpenProcess failed for pid {pid} (err {ctypes.get_last_error()}). "
                "Run this from an elevated shell."
            )
        self.k32 = k32
        self.base = self._module_base(psapi, exe_name)
        if not self.base:
            raise RuntimeError("could not locate the exe module base")

    @staticmethod
    def _find_pid(exe_name):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class ENTRY(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]

        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        entry = ENTRY(); entry.dwSize = ctypes.sizeof(ENTRY)
        found = None
        if k32.Process32First(snap, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.decode("latin1").lower() == exe_name.lower():
                    found = entry.th32ProcessID
                    break
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
        k32.CloseHandle(snap)
        return found

    def _module_base(self, psapi, exe_name):
        arr = (ctypes.c_void_p * 1024)()
        needed = wt.DWORD()
        # LIST_MODULES_ALL = 3
        if not psapi.EnumProcessModulesEx(self.h, ctypes.byref(arr), ctypes.sizeof(arr),
                                          ctypes.byref(needed), 3):
            return None
        buf = ctypes.create_string_buffer(260)
        for i in range(needed.value // ctypes.sizeof(ctypes.c_void_p)):
            psapi.GetModuleBaseNameA(self.h, arr[i], buf, 260)
            if buf.value.decode("latin1").lower() == exe_name.lower():
                return arr[i]
        return None

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = self.k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf,
                                        ctypes.c_size_t(size), ctypes.byref(got))
        return buf.raw[:got.value] if ok and got.value == size else None

    @property
    def source(self):
        return f"live process (pid {self.pid})"


class DumpReader:
    """Reads a full-memory minidump."""

    def __init__(self, path):
        from minidump.minidumpfile import MinidumpFile
        self.mf = MinidumpFile.parse(path)
        self.rdr = self.mf.get_reader()
        self.path = path
        self.base = None
        for m in self.mf.modules.modules:
            name = m.name.split("\\")[-1]
            if "FINAL MIX" in name.upper() and name.lower().endswith(".exe"):
                self.base = m.baseaddress
                break
        if self.base is None:
            raise RuntimeError("exe module not found in dump")

    def read(self, addr, size):
        try:
            return self.rdr.read(addr, size)
        except Exception:
            return None

    @property
    def source(self):
        return os.path.basename(self.path)


# --------------------------------------------------------------------------- model
def classify(slot):
    """The distinction that matters: a slot can be LOADED yet carry no owner stamp.

    Release wipes the blob but leaves state and the cached name behind, so a valid blob
    header is what separates 'still live' from 'released, safe to reuse'."""
    owner, state, name, sec4 = slot["owner"], slot["state"], slot["name"], slot["sec4"]
    # blob+4 is exactly 128 on every valid header seen; a released slot reads 0 or leftover garbage,
    # and its runLen is stale too -- trusting it invents phantom runs over real blocks.
    live_data = state == STATE_READY and bool(name) and sec4 == BLOB_HEADER_FIRST_SECTION
    if owner == UNCLAIMED:
        if live_data:
            return "live_unowned"          # loading over this clobbers live data
        if state == STATE_READY and name:
            return "released"              # stale metadata, blob wiped -- genuinely free
        return "free"
    if owner == slot["index"]:
        return "primary"
    return "member"


def collect(reader, ours):
    base = reader.base
    slots = []
    raw = reader.read(base + RVA_SLOT_TABLE, SLOT_STRIDE * SLOT_COUNT)
    for i in range(SLOT_COUNT):
        if raw is None:
            slots.append({"index": i, "owner": UNCLAIMED, "runLen": 0, "flags": 0,
                          "state": 0, "name": "", "sec4": 0, "kind": "free", "ours": False})
            continue
        rec = raw[i * SLOT_STRIDE:(i + 1) * SLOT_STRIDE]
        name = rec[4:0x24].split(b"\x00")[0].decode("latin1", "replace")
        hdr = reader.read(base + RVA_BLOB_TABLE + i * BLOB_STRIDE + 4, 4)
        sec4 = struct.unpack("<i", hdr)[0] if hdr else 0
        s = {"index": i, "owner": rec[0], "runLen": rec[1], "flags": rec[2], "state": rec[3],
             "name": name, "sec4": sec4,
             "ours": name.replace(".mdls", "") in ours}
        s["kind"] = classify(s)
        slots.append(s)

    # Runs: a primary spans runLen slots. An unowned-but-live slot still implies a span.
    runs, cover = [], [0] * SLOT_COUNT
    for s in slots:
        if s["kind"] in ("primary", "live_unowned") and s["runLen"] > 0:
            span = [j for j in range(s["index"], min(s["index"] + s["runLen"], SLOT_COUNT))]
            runs.append({"start": s["index"], "len": len(span), "name": s["name"],
                         "kind": s["kind"], "ours": s["ours"]})
            for j in span:
                cover[j] += 1
    for s in slots:
        s["overlap"] = cover[s["index"]] > 1

    ents = []
    for i in range(ENT_COUNT):
        rec = reader.read(base + RVA_ENTITY_POOL + i * ENT_STRIDE, 0x380)
        if rec is None:
            ents.append({"index": i, "id": 0, "cat": 0, "live": False})
            continue
        eid = struct.unpack_from("<I", rec, 4)[0]
        f374 = struct.unpack_from("<I", rec, 0x374)[0]
        ents.append({"index": i, "id": eid, "cat": (eid >> 16) & 0xFFFF,
                     "live": (f374 & 3) == 1 and eid not in (0, 0xFFFFFFFF)})

    braw = reader.read(base + RVA_BUCKET_TABLE, 8 * BUCKET_COUNT)
    buckets = []
    for i in range(BUCKET_COUNT):
        v = struct.unpack_from("<Q", braw, i * 8)[0] if braw else 0xFFFFFFFFFFFFFFFF
        buckets.append({"index": i, "value": v, "claimed": v != 0xFFFFFFFFFFFFFFFF})

    return {"base": base, "source": reader.source, "slots": slots, "runs": runs,
            "entities": ents, "buckets": buckets}


def parse_log(path):
    """Model stems this session's log shows US loading, so ours can be told from natives."""
    if not path or not os.path.exists(path):
        return set()
    pat = re.compile(r"recorded triggered load .*model=([A-Za-z0-9_]+)\.mdls")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return set(pat.findall(fh.read()))


# --------------------------------------------------------------------------- render
HTML = r"""<title>KH1 Slot Map</title>
<style>
:root{
  --bg:#F2F4F7; --surface:#FFFFFF; --line:#D9DFE7; --text:#1A2028; --muted:#5C6879;
  --accent:#0E8C7D;
  --free:#C3CBD6; --primary:#3E9E6E; --member:#3B7FA3; --unowned:#D08A2C; --overlap:#D24C55;
  --shadow:0 1px 2px rgba(16,24,32,.06),0 8px 24px rgba(16,24,32,.05);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0F1216; --surface:#161B22; --line:#232A33; --text:#D6DDE6; --muted:#8592A3;
  --accent:#5FD3C4;
  --free:#3E4A57; --primary:#4C9F70; --member:#2F6F8F; --unowned:#E0A458; --overlap:#E06C75;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
}}
:root[data-theme="dark"]{
  --bg:#0F1216; --surface:#161B22; --line:#232A33; --text:#D6DDE6; --muted:#8592A3;
  --accent:#5FD3C4;
  --free:#3E4A57; --primary:#4C9F70; --member:#2F6F8F; --unowned:#E0A458; --overlap:#E06C75;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  font-variant-numeric:tabular-nums;}
.mono{font-family:ui-monospace,"Cascadia Mono","SF Mono","JetBrains Mono",Consolas,monospace}
.wrap{max-width:1180px;margin:0 auto;padding:32px 24px 72px}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:baseline;justify-content:space-between;
  padding-bottom:14px;border-bottom:1px solid var(--line);margin-bottom:26px}
h1{margin:0;font-size:20px;letter-spacing:-.01em;font-weight:650}
h2{margin:0 0 4px;font-size:13px;font-weight:650;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted)}
.sub{color:var(--muted);font-size:13px}
section{margin-bottom:34px}
.note{color:var(--muted);font-size:13px;margin:0 0 14px;max-width:66ch}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:18px;
  box-shadow:var(--shadow)}
.scroll{overflow-x:auto}
/* summary */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;margin-bottom:26px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
  box-shadow:var(--shadow)}
.stat b{display:block;font-size:26px;font-weight:650;letter-spacing:-.02em;line-height:1.1}
.stat span{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.07em}
.stat.warn b{color:var(--unowned)} .stat.bad b{color:var(--overlap)}
/* slot strip */
.strip{display:grid;grid-template-columns:repeat(64,1fr);gap:2px;min-width:900px}
.cell{position:relative;aspect-ratio:1/1.5;border-radius:3px;background:var(--free);
  display:flex;align-items:center;justify-content:center;font-size:9px;color:#0B0E12;
  font-family:ui-monospace,Consolas,monospace;cursor:default;border:1px solid transparent}
.cell.primary{background:var(--primary)} .cell.member{background:var(--member);color:#EAF2F7}
.cell.live_unowned{background:var(--unowned)} .cell.released{background:var(--free);opacity:.55}
.cell.free{background:var(--free);opacity:.35}
.cell.overlap{border-color:var(--overlap);box-shadow:0 0 0 1px var(--overlap)}
.cell.ours::after{content:"";position:absolute;left:2px;right:2px;bottom:2px;height:2px;
  border-radius:2px;background:var(--accent)}
.cell:hover{outline:2px solid var(--text);outline-offset:1px;z-index:3}
.ticks{display:grid;grid-template-columns:repeat(64,1fr);gap:2px;min-width:900px;margin-top:4px}
.tick{font:10px/1.4 ui-monospace,Consolas,monospace;color:var(--muted);text-align:center}
/* run bars */
.runs{position:relative;min-width:900px;margin-top:10px}
.runrow{position:relative;height:17px;margin-bottom:3px}
.runbar{position:absolute;height:15px;border-radius:3px;border:1px solid var(--line);
  background:var(--surface);font:10px/15px ui-monospace,Consolas,monospace;color:var(--text);
  padding:0 5px;white-space:nowrap;overflow:hidden}
.runbar.live_unowned{border-color:var(--unowned)}
.runbar.primary{border-color:var(--primary)}
.runbar.clash{background:color-mix(in srgb,var(--overlap) 22%,var(--surface));
  border-color:var(--overlap)}
/* legend */
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:16px;font-size:12.5px;color:var(--muted)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;
  vertical-align:-1px}
/* pool */
.pool{display:grid;grid-template-columns:repeat(24,1fr);gap:3px;min-width:660px}
.ent{aspect-ratio:1;border-radius:3px;background:var(--free);opacity:.3;
  font:8px/1 ui-monospace,Consolas,monospace;display:flex;align-items:center;justify-content:center;
  color:#0B0E12}
.ent.live{opacity:1}
.ent.c3{background:var(--accent)} .ent.c4{background:var(--member)}
.ent.c5{background:var(--primary)} .ent.cx{background:var(--unowned)}
/* buckets */
.bk{display:grid;grid-template-columns:repeat(32,1fr);gap:3px;min-width:520px}
.bkc{aspect-ratio:1;border-radius:3px;background:var(--free);opacity:.3}
.bkc.on{background:var(--accent);opacity:1}
/* table */
table{border-collapse:collapse;width:100%;font-size:13px;min-width:720px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
  font-weight:650;padding:0 10px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;
  border:1px solid var(--line)}
.pill.primary{color:var(--primary);border-color:var(--primary)}
.pill.member{color:var(--member);border-color:var(--member)}
.pill.live_unowned{color:var(--unowned);border-color:var(--unowned)}
.pill.free,.pill.released{color:var(--muted)}
.flag{color:var(--overlap);font-weight:650}
</style>
<div class="wrap">
  <header>
    <div>
      <h1>KH1 Slot Map</h1>
      <div class="sub">Species resource slots, entity pool and handle buckets</div>
    </div>
    <div class="sub mono" id="meta"></div>
  </header>
  <div class="stats" id="stats"></div>

  <section>
    <h2>Species slot table</h2>
    <p class="note">64 slots of 0x50 bytes. A creature claims a <em>run</em> of consecutive slots.
      Amber slots are loaded and carry live data but no owner stamp &mdash; those read as free if you
      only test the owner byte, and loading over one clobbers a creature in use. A cyan underline
      marks a model this session's log shows us loading.</p>
    <div class="card scroll">
      <div class="strip" id="strip"></div>
      <div class="ticks" id="ticks"></div>
      <div class="runs" id="runs"></div>
    </div>
    <div class="legend">
      <span><i style="background:var(--primary)"></i>primary (owner == index)</span>
      <span><i style="background:var(--member)"></i>run member</span>
      <span><i style="background:var(--unowned)"></i>loaded, unowned &mdash; NOT free</span>
      <span><i style="background:var(--free);opacity:.55"></i>released (blob wiped)</span>
      <span><i style="background:var(--free);opacity:.35"></i>free</span>
      <span><i style="box-shadow:0 0 0 1px var(--overlap);background:transparent"></i>overlapped</span>
    </div>
  </section>

  <section>
    <h2>Entity pool</h2>
    <p class="note">96 slots of 1200 bytes, coloured by category, dimmed when not enumerable.</p>
    <div class="card scroll"><div class="pool" id="pool"></div></div>
  </section>

  <section>
    <h2>Resource handle buckets</h2>
    <p class="note">64 entries. Unclaimed reads -1, and resolving a handle into one returns that -1
      raw.</p>
    <div class="card scroll"><div class="bk" id="bk"></div></div>
  </section>

  <section>
    <h2>Occupied slots</h2>
    <div class="card scroll"><table id="tbl">
      <thead><tr><th>Slot</th><th>State</th><th>Owner</th><th>Run</th><th>blob+4</th>
        <th>Model</th><th>Notes</th></tr></thead><tbody></tbody></table></div>
  </section>
</div>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
const label = {primary:"primary",member:"member",live_unowned:"loaded, unowned",
               released:"released",free:"free"};

$("#meta").textContent = D.source + "  \u00b7  exe base 0x" + D.base.toString(16).toUpperCase();

const unowned = D.slots.filter(s=>s.kind==="live_unowned").length;
const overlaps = D.slots.filter(s=>s.overlap).length;
const freeN = D.slots.filter(s=>s.kind==="free"||s.kind==="released").length;
const liveEnts = D.entities.filter(e=>e.live).length;
const claimed = D.buckets.filter(b=>b.claimed).length;
$("#stats").innerHTML = [
  ["Free slots", freeN + " / 64", ""],
  ["Loaded, unowned", unowned, unowned ? "warn" : ""],
  ["Overlapped slots", overlaps, overlaps ? "bad" : ""],
  ["Live entities", liveEnts + " / 96", ""],
  ["Buckets claimed", claimed + " / 64", ""],
].map(([k,v,c])=>`<div class="stat ${c}"><b>${v}</b><span>${k}</span></div>`).join("");

$("#strip").innerHTML = D.slots.map(s=>{
  const cls = [ "cell", s.kind, s.overlap?"overlap":"", s.ours?"ours":"" ].join(" ");
  const t = `slot ${s.index}\nkind: ${label[s.kind]}\nowner: 0x${s.owner.toString(16).toUpperCase().padStart(2,"0")}`
          + `\nrunLen: ${s.runLen}\nstate: ${s.state}\nblob+4: ${s.sec4}`
          + (s.name?`\nmodel: ${s.name}`:"") + (s.overlap?"\n\u26a0 covered by more than one run":"");
  return `<div class="${cls}" title="${t}">${s.index}</div>`;
}).join("");
$("#ticks").innerHTML = D.slots.map(s=>`<div class="tick">${s.index%8===0?s.index:""}</div>`).join("");

// Pack runs onto rows so overlapping spans are forced apart and read as a collision.
const rows=[];
D.runs.slice().sort((a,b)=>a.start-b.start).forEach(r=>{
  let ri = rows.findIndex(row => row.every(x => r.start >= x.start+x.len || r.start+r.len <= x.start));
  if (ri < 0) { rows.push([r]); } else { rows[ri].push(r); }
});
const clash = new Set();
D.runs.forEach(a=>D.runs.forEach(b=>{
  if(a!==b && a.start < b.start+b.len && b.start < a.start+a.len){clash.add(a);clash.add(b);}
}));
$("#runs").innerHTML = rows.map(row=>`<div class="runrow">` + row.map(r=>{
  const l = (r.start/64*100).toFixed(4), w = (r.len/64*100).toFixed(4);
  const cls = ["runbar", r.kind, clash.has(r)?"clash":""].join(" ");
  return `<div class="${cls}" style="left:${l}%;width:calc(${w}% - 2px)" `
       + `title="${r.name||"(unnamed)"} \u2014 slots ${r.start}..${r.start+r.len-1}">`
       + `${r.start}\u2013${r.start+r.len-1} ${r.name||""}</div>`;
}).join("") + `</div>`).join("");

$("#pool").innerHTML = D.entities.map(e=>{
  const c = e.cat===3?"c3":e.cat===4?"c4":e.cat===5?"c5":"cx";
  return `<div class="ent ${e.live?"live":""} ${c}" title="slot ${e.index}\nid 0x`
       + `${e.id.toString(16).toUpperCase()}\ncategory ${e.cat}\n${e.live?"live":"not enumerable"}">`
       + `${e.index}</div>`;
}).join("");

$("#bk").innerHTML = D.buckets.map(b=>`<div class="bkc ${b.claimed?"on":""}" title="bucket ${b.index}\n`
  + `${b.claimed?"0x"+b.value.toString(16).toUpperCase():"unclaimed (-1)"}"></div>`).join("");

$("#tbl tbody").innerHTML = D.slots.filter(s=>s.kind!=="free").map(s=>{
  const notes=[];
  if(s.kind==="live_unowned") notes.push('<span class="flag">unowned but live</span>');
  if(s.overlap) notes.push('<span class="flag">overlapped</span>');
  if(s.ours) notes.push("ours");
  return `<tr><td class="mono">${s.index}</td>`
    + `<td><span class="pill ${s.kind}">${label[s.kind]}</span></td>`
    + `<td class="mono">0x${s.owner.toString(16).toUpperCase().padStart(2,"0")}</td>`
    + `<td class="mono">${s.runLen}</td><td class="mono">${s.sec4}</td>`
    + `<td class="mono">${s.name||"\u2014"}</td><td>${notes.join(" \u00b7 ")||"\u2014"}</td></tr>`;
}).join("") || `<tr><td colspan="7">No occupied slots.</td></tr>`;
</script>
"""


def main():
    ap = argparse.ArgumentParser(description="Render KH1 slot/entity state as an HTML page.")
    ap.add_argument("--dump", help="full crash dump to read instead of the live process")
    ap.add_argument("--log", help="kh1_native.log, to mark which models we loaded")
    ap.add_argument("-o", "--out", default="slotmap.html")
    args = ap.parse_args()

    reader = DumpReader(args.dump) if args.dump else LiveReader()
    data = collect(reader, parse_log(args.log))
    html = HTML.replace("__DATA__", json.dumps(data))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    warn = sum(1 for s in data["slots"] if s["kind"] == "live_unowned")
    over = sum(1 for s in data["slots"] if s["overlap"])
    print(f"read {reader.source}; exe base 0x{data['base']:X}")
    print(f"loaded-but-unowned slots: {warn}   overlapped slots: {over}")
    print(f"wrote {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()

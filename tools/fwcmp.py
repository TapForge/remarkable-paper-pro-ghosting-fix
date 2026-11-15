"""Static check of another xochitl build: are the places ggsense hooks still the same code?

usage: python3 tools/fwcmp.py REF_XOCHITL OTHER_XOCHITL [--show N]

REF_XOCHITL is /usr/bin/xochitl of software 3.29.1.0 (the build the addresses in ggsense were
read from), OTHER_XOCHITL the program of the build to check. Copy a tablet's program with
    scp remarkable:/usr/bin/xochitl xochitl-VERSION
Needs Python 3 with numpy and pyelftools (python3 -m pip install numpy pyelftools).

For every function anchor the first instructions of the reference are turned into a
signature (PC-relative fields and address-dependent immediates masked) and searched in the
other build's executable segment. A unique hit is then compared instruction by
instruction over the whole function. Data anchors are re-derived from the code that
references them (ADRP + ADD/LDR pairs) inside matched code, and the vtable from the
relocation that points to swapBuffers_impl.

Reading the result (docs/BUILDING.md, "Checking another build"):
  every function "identical" with shift +0x0 and every data anchor with shift +0x0
      -> the constants of ggsense hold for that build; only the build-id has to be added
  identical code, but shifted
      -> the constants must be replaced by the printed ones (and the signature arrays re-read)
  functions that differ or are not found
      -> a port: the code has to be read again
Nothing here executes the binaries; they are read as bytes. A match says the code is the
same. It does not replace a run on a real tablet.
"""
import sys

import numpy as np
from elftools.elf.elffile import ELFFile

# ---- anchors of ggsense 1.8.32 (addresses in the 3.29.1 build) ----
FUNCS = [
    ("swapBuffers_impl (XO_IMPL)", 0x86a9f0),
    ("sendUpdate (XO_SEND)", 0x86a910),
    ("ghostControl (XO_GHOSTCTL)", 0x86a8d0),
    ("enqueue (XO_UPREP)", 0x8694b0),
    ("commit (XO_UCOMMIT)", 0x865780),
    ("sync (XO_SYNC)", 0x8626e0),
    # not called by ggsense, but they define the layouts it reads (update nodes, lists, SWTCON object)
    ("swapBuffers wrapper", 0x85e2e0),
    ("clipper", 0x864850),
    ("dependency builder", 0x862780),
    ("frame generator thread", 0x867ad0),
    ("kernel dispatcher", 0x874190),
    ("generic kernel", 0x870f80),
    ("pixel converter", 0x876640),
]
DATA = [
    ("EPFramebuffer::s_instance (XO_S_INSTANCE)", 0x1861668),
    ("EPFramebufferAcep2 vtable (XO_VT_ACEP2)", 0x1800ab0),
    ("SWTCON global, GOT slot (XO_SW_GOT)", 0x1850fb0),
    ("temperature (XO_TEMP)", 0x1840bc8),
    ("state buffer pointer (XO_STATEP)", 0x1861f20),
]
IMPL_SLOT = 23


class Img:
    def __init__(self, path):
        self.data = open(path, "rb").read()
        elf = ELFFile(open(path, "rb"))
        self.load = []
        for seg in elf.iter_segments():
            if seg["p_type"] == "PT_LOAD":
                self.load.append((seg["p_vaddr"], seg["p_offset"], seg["p_filesz"], seg["p_memsz"], seg["p_flags"]))
        ex = [s for s in self.load if s[4] & 1]
        self.tv, self.to, self.ts = ex[0][0], ex[0][1], ex[0][2] & ~3
        self.text = np.frombuffer(self.data, dtype="<u4", count=self.ts // 4, offset=self.to)
        self.bid = None
        for sec in elf.iter_sections():
            if sec.name == ".note.gnu.build-id":
                for note in sec.iter_notes():
                    self.bid = note["n_desc"]
        # relative relocations: where a pointer to X is stored
        self.rel = {}
        for sec in elf.iter_sections():
            if sec.name == ".rela.dyn":
                raw = np.frombuffer(sec.data(), dtype="<u8").reshape(-1, 3)
                m = (raw[:, 1] & 0xFFFFFFFF) == 1027  # R_AARCH64_RELATIVE
                self.rel_off = raw[m, 0]
                self.rel_add = raw[m, 2]
        self.sections = {s.name: (s["sh_addr"], s["sh_size"]) for s in elf.iter_sections() if s["sh_addr"]}
        # function starts: every BL target
        bl = (self.text & 0xFC000000) == 0x94000000
        idx = np.nonzero(bl)[0].astype(np.int64)
        imm = (self.text[bl] & 0x03FFFFFF).astype(np.int64)
        imm = np.where(imm & 0x02000000, imm - 0x04000000, imm)
        tgt = self.tv + (idx + imm) * 4
        tgt = tgt[(tgt >= self.tv) & (tgt < self.tv + self.ts)]
        # ... and every code address a relocation points to (virtual functions, callbacks)
        ra = self.rel_add[(self.rel_add >= self.tv) & (self.rel_add < self.tv + self.ts) & (self.rel_add % 4 == 0)].astype(np.int64)
        self.fstarts = np.unique(np.concatenate([tgt, ra]))

    def insn(self, addr):
        return int(self.text[(addr - self.tv) // 4])

    def fend(self, addr):
        """Address of the next known function start after addr."""
        i = np.searchsorted(self.fstarts, addr, side="right")
        return int(self.fstarts[i]) if i < len(self.fstarts) else self.tv + self.ts

    def u64(self, vaddr):
        for v, o, fs, ms, fl in self.load:
            if v <= vaddr < v + fs:
                return int.from_bytes(self.data[o + vaddr - v:o + vaddr - v + 8], "little")
        return None


def pc_mask(i):
    """Bits of an instruction that depend on where code and data sit (to ignore)."""
    if (i & 0x7C000000) == 0x14000000:   # b, bl
        return 0x03FFFFFF
    if (i & 0xFF000010) == 0x54000000:   # b.cond
        return 0x00FFFFE0
    if (i & 0x7E000000) == 0x34000000:   # cbz, cbnz
        return 0x00FFFFE0
    if (i & 0x7E000000) == 0x36000000:   # tbz, tbnz
        return 0x0007FFE0
    if (i & 0x1F000000) == 0x10000000:   # adr, adrp
        return 0x60FFFFE0
    if (i & 0x3B000000) == 0x18000000:   # ldr literal
        return 0x00FFFFE0
    return 0


def masks_for(words):
    """Per-instruction ignore masks for a run of reference instructions, tracking ADRP pages:
    the low-12 immediates that complete an ADRP address are address-dependent too."""
    page = set()
    out = []
    for i in words:
        i = int(i)
        m = pc_mask(i)
        rd, rn = i & 31, (i >> 5) & 31
        if (i & 0x9F000000) == 0x90000000:          # adrp rd
            page.add(rd)
        elif (i & 0x7F800000) == 0x11000000 and rn in page:   # add rd, rn, #imm12 (completes the address)
            m |= 0x003FFC00
            page.discard(rn)
            page.discard(rd)
        elif (i & 0x3B000000) == 0x39000000 and rn in page:   # ldr/str [rn, #imm12]: :lo12: of a global
            m |= 0x003FFC00
            if (i & 0x00400000) and rd == rn:        # a load that overwrites the page register
                page.discard(rn)
        elif (i & 0x1F000000) == 0x10000000:         # adr
            page.discard(rd)
        out.append(m)
    return out


def find_sig(ref, other, addr, k=14):
    """Addresses in `other` where the first k instructions at ref:addr match under the masks."""
    w = [ref.insn(addr + 4 * j) for j in range(k)]
    ms = masks_for(w)
    n = len(other.text) - k
    cand = np.ones(n, dtype=bool)
    for j in range(k):
        keep = np.uint32(~ms[j] & 0xFFFFFFFF)
        cand &= (other.text[j:j + n] & keep) == (np.uint32(w[j]) & keep)
        if not cand.any():
            break
    return [other.tv + int(x) * 4 for x in np.nonzero(cand)[0]]


def compare_fn(ref, other, ra, oa):
    """Walk both functions in step. Returns (length, matched_until, list of differing offsets)."""
    n = (ref.fend(ra) - ra) // 4
    w = [ref.insn(ra + 4 * j) for j in range(n)]
    ms = masks_for(w)
    diffs = []
    for j in range(n):
        if oa + 4 * j >= other.tv + other.ts:
            diffs.append(j)
            break
        o = other.insn(oa + 4 * j)
        keep = ~ms[j] & 0xFFFFFFFF
        if (o & keep) != (w[j] & keep):
            diffs.append(j)
    return n, diffs


def adrp_pairs(img, target):
    """Code sites in img that build `target` with ADRP + (ADD | LDR/STR) within 4 instructions."""
    t = img.text
    page = target & ~0xFFF
    lo = target & 0xFFF
    is_adrp = (t & 0x9F000000) == 0x90000000
    idx = np.nonzero(is_adrp)[0].astype(np.int64)
    w = t[is_adrp].astype(np.int64)
    imm = ((w >> 5) & 0x7FFFF) << 2 | ((w >> 29) & 3)
    imm = np.where(imm & (1 << 20), imm - (1 << 21), imm)
    pc = img.tv + idx * 4
    pages = (pc & ~0xFFF) + (imm << 12)
    hits = idx[pages == page]
    sites = []
    for h in hits:
        rd = int(t[h]) & 31
        for d in range(1, 5):
            if h + d >= len(t):
                break
            i = int(t[h + d])
            rn = (i >> 5) & 31
            if (i & 0x7F800000) == 0x11000000 and rn == rd and ((i >> 10) & 0xFFF) == lo:
                sites.append((img.tv + int(h) * 4, d, "add"))
                break
            if (i & 0x3B000000) == 0x39000000 and rn == rd:
                size = (i >> 30) & 3
                if (((i >> 10) & 0xFFF) << size) == lo:
                    sites.append((img.tv + int(h) * 4, d, "mem"))
                    break
            if (i & 31) == rd and (i & 0x9F000000) == 0x90000000:
                break
    return sites


def decode_pair(img, addr, d):
    """The address built by the ADRP at addr and the instruction d slots later."""
    a = img.insn(addr)
    imm = ((a >> 5) & 0x7FFFF) << 2 | ((a >> 29) & 3)
    if imm & (1 << 20):
        imm -= 1 << 21
    page = (addr & ~0xFFF) + (imm << 12)
    i = img.insn(addr + 4 * d)
    if (i & 0x7F800000) == 0x11000000:
        return page + ((i >> 10) & 0xFFF)
    if (i & 0x3B000000) == 0x39000000:
        return page + (((i >> 10) & 0xFFF) << ((i >> 30) & 3))
    return None


def main():
    ref, other = Img(sys.argv[1]), Img(sys.argv[2])
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 6
    print(f"reference build-id {ref.bid}  text {ref.ts:#x} bytes, {len(ref.fstarts)} functions")
    print(f"other     build-id {other.bid}  text {other.ts:#x} bytes, {len(other.fstarts)} functions")
    if ref.bid == other.bid:
        print("SAME BUILD")
    fmap = {}
    print("\n== functions ==")
    ok_f = 0
    for name, ra in FUNCS:
        hits = find_sig(ref, other, ra)
        if len(hits) != 1:
            # retry with a longer or shorter signature before giving up
            for k in (24, 8):
                h2 = find_sig(ref, other, ra, k)
                if len(h2) == 1:
                    hits = h2
                    break
        if len(hits) != 1:
            print(f"  {name:34s} {ra:#x}: {'NOT FOUND' if not hits else str(len(hits)) + ' candidates'}")
            continue
        oa = hits[0]
        n, diffs = compare_fn(ref, other, ra, oa)
        fmap[ra] = oa
        state = "identical" if not diffs else f"{len(diffs)} of {n} instructions differ (first at +{diffs[0]*4:#x})"
        ok_f += not diffs
        print(f"  {name:34s} {ra:#x} -> {oa:#x} (shift {oa-ra:+#x}), {n} instructions: {state}")
        for j in diffs[:show]:
            print(f"      +{j*4:#06x}: ref {ref.insn(ra+4*j):08x}  other {other.insn(oa+4*j):08x}")
    print(f"  => {ok_f} of {len(FUNCS)} functions identical after masking position-dependent fields")

    print("\n== data ==")
    ok_d = 0
    for name, da in DATA:
        sites = adrp_pairs(ref, da)
        found = {}
        used = 0
        for sa, d, kind in sites[:400]:
            # a window of code around the site, searched in the other build
            start = sa - 16
            hits = find_sig(ref, other, start, k=12)
            if len(hits) != 1:
                continue
            na = decode_pair(other, hits[0] + 16, d)
            if na is not None:
                found[na] = found.get(na, 0) + 1
                used += 1
        if not found:
            print(f"  {name:44s} {da:#x}: {len(sites)} reference sites, none re-found")
            continue
        best = max(found, key=found.get)
        agree = found[best]
        ok_d += len(found) == 1
        print(f"  {name:44s} {da:#x} -> {best:#x} (shift {best-da:+#x}); {agree} of {used} re-found sites agree"
              + ("" if len(found) == 1 else f"; OTHER ANSWERS {sorted(hex(k) for k in found if k != best)}"))
    # vtable: the slot that holds swapBuffers_impl
    if 0x86a9f0 in fmap:
        impl = fmap[0x86a9f0]
        slots = other.rel_off[other.rel_add == impl]
        print(f"  pointers to the new swapBuffers_impl ({impl:#x}) are stored at: {[hex(int(s)) for s in slots]}"
              f"  -> vtable address point candidates {[hex(int(s) - IMPL_SLOT * 8) for s in slots]}")
    print(f"  => {ok_d} of {len(DATA)} data anchors re-derived unambiguously")


if __name__ == "__main__":
    main()

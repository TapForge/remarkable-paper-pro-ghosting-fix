# Building GhostGuard from source, and porting it

You do not need this to use GhostGuard: built engines are in `prebuilt/` (`ggsense.so` for software 3.29.1,
`ggsense-3.29.0.so` for software 3.29.0).

## Build

Needs a C compiler on the computer for the tests (`cc`: Xcode command line tools on macOS, `build-essential` on
Linux) and the zig compiler as a Python package for the tablet binaries (`python3 -m pip install ziglang`).

```bash
bash ggsense/build.sh host
```

compiles and runs the unit tests and the fuzz test on the computer (address and undefined-behaviour sanitizers on).
The last lines should read `ALL OK (0 failures)` and `invariant violations: 0`.

```bash
bash ggsense/build.sh
```

runs the same tests and then builds `prebuilt/ggsense.so`, `prebuilt/ggsense-3.29.0.so` and `tools/ggtool` for the
tablet (aarch64, glibc 2.31). The two libraries are one source: `-DGG_XO_3290` swaps the build-id of `xochitl` the
library expects, and nothing else. The build is reproducible: with the same zig version (0.17.1 for the files
in `prebuilt/`) it gives the same bytes.
`tools/tabset.c` (a test tool that writes cells of a live waveform table through `/proc/PID/mem`) is built the same
way when you need it; the installer does not use it.

Notes:

- The prebuilt library is the same source built with `-O2 -s`. The copy on the developer's tablet was built
  without `-s`, so its checksum differs from the one here; the code is the same.
- A few unit tests compare against measurement files and a dump of the tablet's waveform table. Those files are
  proprietary or personal and are not in this repository; without them these tests print
  `(no TESTDATA: ...)` and are skipped.

Install your own build with `bash install.sh` (it takes the library for the tablet's software from `prebuilt/`),
or directly (this line is for software 3.29.1; for another known build add `--os` and `--xo-md5`, see
`bash deploy/persist_install.sh --help`):

```bash
bash deploy/persist_install.sh --go --so prebuilt/ggsense.so --clock tools/clock14f.js --extra tools/bwturn.js --extra tools/ggrefresh.js --extra tools/viewchange.js --extra tools/settingskb.js
```

Leave out the `--extra` options for no start-up extras, or give only the ones you want.

The boot scripts pin the checksum of the library they were installed with; a library copied over it by hand is
refused at the next boot. Always go through the installer.

## How the pieces fit

| Piece | Role |
|---|---|
| `ggsense/ggsense-1.8.32.c` | The engine: one C file, loaded into `xochitl` with `LD_PRELOAD` through XOVI's service drop-in. Its header comment (about 400 lines) is the design history and the list of runtime keys. |
| `ghostguard/ghostguard.qmd` | A qt-resource-rebuilder patch: adds a small QML object to the interface that reads `/home/root/ghostguard/control.json` every 3 s and evaluates the command in it. This is the command channel (`tools/ggq.py`). |
| `tools/clock14f.js`, `tools/kbd_overlay.js` | QML snippets sent at start: a clock that tells the engine when the screen has settled, and a watcher for the on-screen keyboard. |
| `tools/bwturn.js`, `tools/ggrefresh.js`, `tools/viewchange.js`, `tools/settingskb.js` | The start-up extras: small JavaScript files sent after every start of `xochitl`, as the last step of `deploy/boot/ggapply.sh`. `deploy/boot/mkpayload.py` turns each into `payload/xNN-<name>.cmd` and `.ok`. `bwturn`, `viewchange` and `settingskb` send engine settings; `ggrefresh` adds the refresh button. |
| `deploy/boot/autostart.sh` | Runs 70 s after boot from a systemd timer: checks everything (the "gates"), waits for 120 s without input, starts XOVI, checks health, starts the crash guard. |
| `deploy/boot/rescue.sh`, `deploy/gg_autorollback.sh` | The way back to stock when the interface fails to start or crashes during the first 15 minutes. |
| `deploy/boot/gg.sh` | `status`, `disable`, `enable`, `stock`, `gates`, `log` on the tablet. |
| `tools/ggtool.c` | Small helper on the tablet: watches the input devices for quiet time, reads memory for diagnostics. |

## What is specific to firmware 3.29.1

Porting to another software version means re-finding all of these. None of it is guesswork-safe: a wrong offset
reads or writes the wrong memory in the tablet's main program.

1. **The build pins.** The list `BUILDS` in `install.sh` (`/etc/version`, the md5 of `/usr/bin/xochitl` and the
   library for that build), `OS_TESTED` and `XO_MD5` at the top of `deploy/boot/autostart.sh` (the installer writes
   the pair of the tablet it found into the copy it sends), and the build ID the engine checks before it reads
   anything (`xo_buildid` in the engine source).
2. **Addresses and layouts inside `xochitl`.** The engine locates the framebuffer object, its image, the
   display-update call and the waveform table objects by offsets that were found by reading this one build. They
   are constants near the top of the engine source, each with a comment on how it was found and which check
   guards it.
3. **The log lines.** The engine learns what the screen is doing from `xochitl`'s own debug log categories
   (`deploy/20-gglog.conf` turns them on). Their wording can change between versions.
4. **The waveform table.** The white tail assumes the STD table of 22–28 °C with 86 frames and the cell layout
   described in [HOW-IT-WORKS.md](HOW-IT-WORKS.md), section 8. The engine measures the table before writing and
   refuses anything that does not look exactly like the stock one, so on a different table it writes nothing.
5. **The QML patch.** `ghostguard.qmd` hooks into a QML file of the interface; qt-resource-rebuilder's hash table
   must be rebuilt after every software update anyway (`xovi/rebuild_hashtable`).

A port should start in observe-only mode: every engine feature can be switched off with runtime keys, and the unit
and fuzz tests run without a tablet.

## Checking another build

Before any of that, find out whether the new build needs a port at all. `tools/fwcmp.py` compares the places
ggsense hooks in two builds of `xochitl` without running them:

```bash
python3 tools/fwcmp.py xochitl-3.29.1.0 xochitl-OTHER
```

(Python 3 with `numpy` and `pyelftools`; get a tablet's program with `scp remarkable:/usr/bin/xochitl xochitl-OTHER`.)
It takes the first instructions of 13 functions of the reference build as signatures, with the fields that depend
on where code and data sit masked out, finds them in the other build and compares each function instruction by
instruction: the six functions ggsense calls or checks (`swapBuffers_impl`, `sendUpdate`, `ghostControl`, enqueue,
commit, sync) and seven that define the layouts it reads (the `swapBuffers` wrapper, the clipper, the dependency
builder, the frame generator, the kernel dispatcher, the generic kernel, the pixel converter). It re-derives the
five data addresses from the code that references them, and the vtable from the relocation that points to
`swapBuffers_impl`.

What it found for the builds next to 3.29.1.0:

| Build | Functions | Data addresses | Result |
|---|---|---|---|
| 3.29.0.153 (`/etc/version` 20261009073202, build-id `c2d96853…e7beba`) | 13 of 13 identical, at the same addresses | 5 of 5 at the same addresses | the constants hold; only the build-id differs |
| 3.28.0.172 (`/etc/version` 20260827113527, build-id `aae326b4…cdbb2a`) | 0 of 13 found identical | 0 of 5 re-found | different display code: a port |

More on 3.29.0.153 against 3.29.1.0, from comparing the two programs and the two root file systems:

- Of 3,407,477 instructions, 3 differ in meaning. All three are the database version of the handwriting search
  index (`sqlite_index_writer.cpp`). About 3,700 more carry only the address of read-only data that moved by
  16 bytes.
- The interface resources in the program are identical: all 799 compressed ones and every plain-text block of
  200 bytes or more that looks like QML, JavaScript, JSON or SVG (486 of them). So the QML that the QML patch and
  the start-up extras hook is the same.
- Qt, the e-paper plug-ins (`libepaper.so`, `libqsgepaper.so`), `xochitl.service` and every waveform file present
  in both are identical.
- Different: the kernel with its modules, the boot loader, the trusted firmware, `xochitl` as described, its PDF
  renderer helper, the sync and telemetry programs, four network libraries, the certificate list, and version and
  licence files. 3.29.1.0 also adds five waveform files for other panel batches.

That is why 3.29.0 is accepted with the same constants: the library for it is the same source with the other
build-id (`-DGG_XO_3290`). It is also why 3.29.0 is marked "not run on a real tablet": the comparison says the
code ggsense reads and calls is the same; it says nothing about the kernel that differs.

To add a build that passes with every function identical and no shift: a `-D` block with its build-id next to
`GG_XO_3290` in the engine source, a line in `ggsense/build.sh`, a line in `BUILDS` in `install.sh`. With
identical code at shifted addresses, the constants and the signature arrays have to be replaced too. Anything
else is a port as described above.

## Development method, for the curious

- Every change was first replayed on the computer against saved journals and state dumps, then fuzzed, then
  installed.
- A fixed camera gave a number for "how much of the previous page is in this one". It was useful to reject bad
  ideas quickly. It was **not** able to confirm good ones: several variants that the camera called clean were
  rejected by the eye. Final decisions were made by reading real documents.
- The tablet was never left to arm an untested build unattended: the boot path falls back to stock on any failed
  check, and two bad boots disable it.

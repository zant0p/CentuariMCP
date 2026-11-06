# Elegoo Centauri Carbon (CC1) — SDCP protocol reference

Everything here was verified against a live CC1 (firmware **V1.4.49**, protocol **V3.0.0**,
mainboard id `60461c580103147000001c0000000000`).

Sources used: Elegoo's own `elegoo-link` SDK, the OpenCentauri project, OrcaSlicer's
`ElegooLink.cpp`, and direct experiments on the printer.

---

## 1. Transport

| Purpose | Endpoint |
|---|---|
| SDCP control (websocket) | `ws://<printer>:3030/websocket` |
| SDCP control (http) | `http://<printer>:3030/` |
| Web UI | `http://<printer>/` |
| File upload | `POST http://<printer>/uploadFile/upload` |
| Camera (MJPEG) | `http://<printer>:3031/video` |
| Discovery (UDP) | broadcast to port 3000 |

Alternative websocket paths that also work: `/ws`, `/`, `/api/websocket`, `/sdcp`.

## 2. Request envelope

```json
{
  "Id": "<uuid>",
  "Topic": "sdcp/request/<mainboardId>",
  "Data": {
    "Cmd": 128,
    "Data": { "...": "command-specific" },
    "RequestID": "<uuid>",
    "MainboardID": "<mainboardId>",
    "TimeStamp": 1699999999999,
    "From": 0
  }
}
```

Two fields vary by command, and getting them wrong matters:

- **`From`** identifies the command source:
  `0 = PC`, `1 = WEB_PC`, `2 = WEB`, `3 = APP`, `4 = SERVER`.
  Most commands use `0`; **start print (128) uses `1`** — as does Elegoo's own client.
- **`TimeStamp`** is Unix seconds for most commands, but **milliseconds for cmd 128**.
- **`RequestID`** should be a **short** id — 16 hex characters. See §2.1 for why
  the length matters.

### 2.1 The RequestID echo is not always verbatim

This is the single most expensive quirk in the protocol, so it gets its own
section.

The printer echoes the `RequestID` back in its reply — but **not always
unchanged**. When the request's `From` is non-zero, the printer **appends a
byte equal to that `From` value** to the echoed `RequestID`.

Measured on a live Centauri Carbon:

| From | TimeStamp | RequestID sent | RequestID echoed |
|------|-----------|----------------|------------------|
| `1` | ms | 36-char dashed uuid | uuid **+ `0x01`** (37 chars) |
| `1` | ms | 16-char hex | 16-char hex, clean |
| `2` | s  | 36-char dashed uuid | uuid **+ `0x02`** |
| `0` | s  | 36-char dashed uuid | 36-char uuid, clean |
| `0` | ms | 36-char dashed uuid | 36-char uuid, clean |

A client that correlates replies on **exact equality** silently drops that
reply, the request never resolves, and the command times out — even though
the printer answered correctly the whole time.

Cmd 128 is the only command sent with `From: 1`, so it is the only command
that ever times out. Every other command goes out with `From: 0` and matches
fine, which is what makes this so confusing to debug: one command in the
whole protocol misbehaves, and it misbehaves as a timeout rather than as an
error.

Two defences, and you want both:

1. **Send a short RequestID.** Every reference implementation does:
   - Elegoo's C++ client (`elegoo-link`): `std::to_string(counter)` → e.g. `"100000"`
   - Elegoo's web UI: 16 hex chars + `Date.now().toString(16)`
   - `pycentauri`: `secrets.token_hex(8)` → 16 hex chars

   A short hex id is echoed back byte-for-byte even with a non-zero `From`.
   (Note that the vendor's own C++ client still does an *exact* match — it
   dodges the bug by using a numeric id and `From: 0`, not by handling it.)

2. **Correlate tolerantly.** Strip trailing control characters from the
   echoed `RequestID`, and fall back to a prefix match. This keeps you
   working even if the printer mangles an echo you did not expect.

Verified against a live printer using a non-existent filename, so no print
could start: cmd 128 with `From: 1` and a 16-char hex id → `Ack 2`, id echoed
exactly; cmd 128 with `From: 1` and a dashed uuid → `Ack 2`, id corrupted.

> The printer serves a **single** websocket client at a time. A second
> connection gets a literal `undefined` back instead of protocol frames, so
> probe the printer through the client's existing connection rather than
> opening a parallel one.

## 3. Response envelope

```json
{
  "Id": "<uuid>",
  "Topic": "sdcp/response/<mainboardId>",
  "Data": {
    "Cmd": 128,
    "Data": { "Ack": 0 },
    "RequestID": "<uuid>",
    "MainboardID": "<mainboardId>",
    "TimeStamp": 1699999999
  }
}
```

`Data.Ack` is `0` on success. For cmd 128 the ack is a **meaningful code**, not just
pass/fail — see §6.

## 4. Status frames

The printer pushes status frames on the same websocket, **not** as a reply to a
command. The payload is at the **top level** of the message, under `Status`:

```json
{
  "Id": "...",
  "Topic": "sdcp/response/<mainboardId>",
  "Status": {
    "CurrentStatus": [1],
    "TempOfNozzle": 210.5,
    "TempOfHotbed": 60.0,
    "LightStatus": { "SecondLight": 1, "RgbLight": [0, 0, 0] },
    "AmsConnectStatus": 1,
    "CameraStatus": 1,
    "NumberOfVideoStreamConnected": 0,
    "MaximumVideoStreamAllowed": 4,
    "PrintInfo": {
      "Status": 13,
      "CurrentLayer": 42,
      "TotalLayer": 325,
      "Progress": 17,
      "Filename": "model.gcode",
      "TaskId": "<uuid>",
      "ErrorNumber": 0,
      "ErrorStatusReason": 0
    }
  }
}
```

Two traps here:

1. **`Status` is top-level.** It is *not* nested under `Data`, and the topic is
   `sdcp/response/...`, not a `/status` topic. Code that looks for
   `msg["Data"]["Status"]` never finds anything.
2. **`CurrentStatus` is the machine state, not the job state.** The printer reports
   `CurrentStatus: [1]` ("printing") while a job is running, and `[0]` ("idle")
   when it is not — but the *job* state is `PrintInfo.Status`. Deriving "is it
   printing?" from `CurrentStatus` gives the wrong answer either way.

### Machine status (`CurrentStatus`)

| Value | Meaning |
|---|---|
| 0 | Idle |
| 1 | Printing |
| 2 | File transferring |
| 3 | Calibrating |
| 4 | Device testing |

### Print status (`PrintInfo.Status`)

| Value | Meaning |
|---|---|
| 0 | Idle |
| 1 | Homing |
| 2 | Dropping (resin) |
| 3 | Exposing (resin) |
| 4 | Lifting (resin) |
| 5 | Pausing |
| 6 | Paused |
| 7 | Stopping |
| 8 | Stopped |
| 9 | Complete |
| 10 | File checking |
| 11 | Printer checking |
| 12 | Resuming |
| **13** | **Printing** |
| **16** | **Preparing** (observed on CC1 while setting up a job) |
| **20** | **Starting** (observed on CC1 while setting up a job) |
| 23 | Auto feeding |
| 24 | Filament unloading |
| 25 | Filament unloading abnormal |
| 26 | Filament unloading paused |

**16 and 20 are the ones people miss.** A job spends its first seconds in those
states, before any layer is laid down. `13` alone only covers actual extrusion.

### Print errors

`PrintInfo.ErrorNumber`:

| Value | Meaning |
|---|---|
| 0 | Normal |
| 1 | File MD5 check failed |
| 2 | File read failed |
| 3 | Resolution mismatch |
| 4 | Format mismatch |
| 5 | Machine model mismatch |

`PrintInfo.ErrorStatusReason`:

| Value | Meaning |
|---|---|
| 0 | OK |
| 1 | Over-temperature (nozzle/bed) |
| 3 | Filament runout |
| 6 | Filament jam |
| 7 | Auto-bed levelling failed |
| 13 / 14 / 23 / 17 | Homing failed (X / Z / Y / general) |
| 18 | Bed adhesion failed |
| 19 | General printing exception |
| 20 | Motor movement abnormality |
| 24 | G-code file error |
| 25 | Camera connection error |
| 26 | Network connection error |
| 27 | Server connection failed |
| 28 | App disconnected during print |
| 12 | USB drive removed |
| 33 / 34 | Nozzle / bed thermistor offline |

## 5. Command reference

| Cmd | Purpose | Body |
|---|---|---|
| 0 | Request status refresh | `{}` — printer replies by **pushing** a status frame |
| 1 | Request attributes | `{}` — replies with `Attributes` |
| 128 | **Start print** | see §6 |
| 129 | Pause print | `{}` |
| 130 | Stop print | `{}` |
| 131 | Resume print | `{}` |
| 132 | Stop material feeding | `{}` |
| 133 | Skip preheating | `{}` |
| 192 | Change printer name | `{ "Name": "..." }` |
| 255 | Terminate file transfer | `{}` |
| 258 | File list | `{ "Url": "/local/" }` → `Data.FileList[]` |
| 259 | Batch delete files | `{ "FileList": ["/local/a.gcode"], "FolderList": [] }` |
| 320 | Print task history | `{}` → `Data.HistoryData[]` (task ids) |
| 321 | Print task details | `{ "TaskId": "..." }` → `Data.HistoryDetailList[]` |
| 324 | **CANVAS status** | `{}` — see §8 |
| 386 | Enable/disable video stream | `{ "Enable": 1 }` → `Data.VideoUrl` |
| 387 | Enable/disable timelapse | `{ "Enable": 1 }` |
| 403 | Set print speed / fans / **light** | see §7 |

### Known spelling errors in the protocol

These are the protocol's own typos and must be sent exactly as written:

- `CurrenCoord` — missing `t` in "Current"
- `RelaseFilmState` — missing `e` in "Release"
- `MaximumCloudSDCPSercicesAllowed` — missing `v` in "Services"

## 6. Starting a print (cmd 128)

```json
{
  "Cmd": 128,
  "Data": {
    "Filename": "/local/model.gcode",
    "StartLayer": 0,
    "Calibration_switch": 0,
    "PrintPlatformType": 0,
    "Tlp_Switch": 0,
    "slot_map": [ { "t": 0, "tray_id": 2, "canvas_id": 0 } ]
  }
}
```

- `Filename` is the **full stored path** (`/local/<name>`), not a bare filename.
- `Calibration_switch` — 1 to auto-level before the print.
- `PrintPlatformType` — 0 = textured (PTE) plate, 1 = smooth (PC) plate.
- `Tlp_Switch` — 1 to record a timelapse.
- `slot_map` — which CANVAS tray feeds which extruder. **Omit it and the printer
  picks a spool itself**, ignoring the colour you asked for. See §8.

> **Cmd 128 and timeouts.** Start print is the one command sent with
> `From: 1`, and it is therefore the one command whose reply can come back
> with a mangled `RequestID` (§2.1). If cmd 128 times out while the file
> is demonstrably on the printer, suspect the correlation, not the file —
> check that the `RequestID` you send is a short hex id, and that the
> reply is matched tolerantly.

### Ack codes for cmd 128

| Ack | Meaning |
|---|---|
| 0 | OK |
| 1 | Printer busy |
| 2 | File not found |
| 3 | File MD5 check failed |
| 4 | File read error |
| 5 | File resolution mismatch |
| 6 | Unrecognised file format |
| 7 | File model mismatch |

### Send it ONCE

**cmd 128 is not idempotent.** The printer can accept a print, queue it, and then
lose the reply — its services restart around a print, so the connection drops
after the command landed. A client that retries then starts a **second print of
the same file**.

Elegoo's own client sends cmd 128 exactly once and never retries it. If you need
retry behaviour, **verify against the printer instead of re-sending**: ask for
status and check whether the printer is now running your file (see the note on
empty `Filename` below).

### `Filename` is empty during the prepare phase

While a job is being set up (print states 16 and 20) the printer reports
`PrintInfo.Filename` as an **empty string**. It only fills in once the job is
under way. So "did my print start?" cannot be answered by matching the filename
alone — match on the *print state* as well (busy + state 16/20/13 = started).

## 7. Lights, fans and speed (cmd 403)

cmd 403 is an overloaded command. Elegoo's own SDK has the light, fan, speed and
temperature variants **commented out** — it only sends the ones it needs. Treat
it with care: a badly-formed body can wedge the printer's control stack.

```json
{ "LightStatus": { "SecondLight": 1 } }   // chamber light on
{ "LightStatus": { "SecondLight": 0 } }   // chamber light off
{ "PrintSpeedPct": 50 }                   // print speed
{ "TargetFanSpeed": { "ModelFan": 50, "AuxiliaryFan": 50, "BoxFan": 50 } }
```

The light state comes back in the status frame as
`Status.LightStatus.SecondLight` (0 = off, 1 = on).

**Do not toggle the light in a tight loop.** The CC's services can wedge if a
write command is sent carelessly; a routine poll that flips the light on and off
every 30 seconds is not a risk worth taking. Turn it on once and leave it.

## 8. CANVAS — the four-spool module

CANVAS is a **4-channel material multiplexer** (Happy-Hare style) with a filament
multiplexer at the toolhead. Each spool carries an **RFID tag** storing brand,
material, **colour**, diameter, weight and nozzle temperatures, so the printer can
identify a spool by itself.

The RFID format is community-reverse-engineered (Elegoo's published spec was
wrong): <https://github.com/DnG-Crafts/ELG-RFID>. Tags are plain NTAG213/215/216
with no encryption.

### Reading the trays (cmd 324)

```json
{
  "Cmd": 324,
  "Data": {
    "Ack": 0,
    "active_canvas_id": 0,
    "active_tray_id": 2,
    "auto_refill": 1,
    "canvas_list": [
      {
        "canvas_id": 0,
        "connected": 1,
        "tray_list": [
          { "tray_id": 0, "brand": "Generic", "filament_type": "PLA",
            "filament_name": "PLA", "filament_code": "0x00000",
            "filament_color": "#898989", "min_nozzle_temp": 190,
            "max_nozzle_temp": 230, "status": 1 },
          { "tray_id": 1, "brand": "ELEGOO", "filament_name": "PLA Wood",
            "filament_color": "#7C4C00", "status": 2 }
        ]
      }
    ]
  }
}
```

- **`status !== 0` means a spool is present.** Status 1 is "ready"; status 2 is
  also a loaded spool (it can mean the tray is in use or moving). Treating only
  `status == 1` as present silently drops a loaded spool.
- `filament_name` is the *product* ("PLA", "PLA Wood"); `filament_color` is the
  hex the printer reports for that spool.
- `min_nozzle_temp` / `max_nozzle_temp` are the spool's temperature window —
  useful for checking a filament preset's temperatures actually fit the spool.

### Choosing a colour

Match on the **colour hex** first, not the filament name. Several trays can hold
the same product — a CC1 commonly has plain "PLA" in grey `#898989`, green
`#077747` and pink `#F95D77` at once. Matching by name picks whichever comes
first.

### Telling the printer which spool to use

The gcode only ever names an *extruder* (`T0`), never a physical spool. On its
own the printer picks a spool itself and ignores the colour you asked for.

The fix is `slot_map` in the cmd 128 body — `SlotMapItem { t, trayId, canvasId }`:

```json
"slot_map": [ { "t": 0, "tray_id": 2, "canvas_id": 0 } ]
```

- `t` — the extruder index (0-based)
- `tray_id` — the CANVAS tray to feed from
- `canvas_id` — the CANVAS unit

**The CC1 supports this** (same code path as the CC2's `config.slot_map`). Verify
it took effect by reading `active_tray_id` back from cmd 324.

## 9. Uploading a file

`POST http://<printer>/uploadFile/upload`, multipart/form-data, **1 MiB chunks**
(1 MiB is the documented maximum — Elegoo's SDK and every third-party client use
it; larger chunks are a plausible cause of the printer's upload handler wedging).

Form fields:

| Field | Meaning |
|---|---|
| `Check` | `1` — enable verification |
| `S-File-MD5` | md5 of the **whole file**, sent on every chunk |
| `Offset` | byte offset of this chunk |
| `Uuid` | transfer id, **one per transfer**, shared by all its chunks |
| `TotalSize` | total file size in bytes |
| `File` | the chunk itself (`application/octet-stream`) |

Success per chunk: `{"code":"000000","success":true}`.

Two traps:

1. **The uuid is a bare hex string** — `uuid4().hex`, no dashes.
2. **Node's `fetch` (undici) cannot read the response.** The printer pads its
   response headers with trailing spaces (`Content-Length: 58         `), which
   undici rejects with `UND_ERR_RES_CONTENT_LENGTH_MISMATCH` — *after* the upload
   has already succeeded. The file lands; only reading the reply fails, so a
   naive client retries and piles up duplicates. Use `requests` (Python) or
   `node:http` (Node), both of which trim the value correctly.

## 10. Camera

`http://<printer>:3031/video` is an MJPEG stream
(`Content-Type: multipart/x-mixed-replace; boundary=--foo`).

Cmd 386 with `{ "Enable": 1 }` returns the URL in `Data.VideoUrl`. The printer
reports `MaximumVideoStreamAllowed` (4 on this unit) and
`NumberOfVideoStreamConnected` — respect the limit.

The stream is dim. The bed plate is a **textured, orange-brown** surface, so
"is the bed orange?" is useless for detecting a part, and the chamber light
switches on and off change the exposure completely.

## 11. The printer's services restart around a print

After a print finishes, the CC's web (80) and SDCP (3030) services sometimes
stop accepting connections while the host still answers ping and ARP with the
same MAC. Ports *refuse* the connection rather than timing out.

This is **not** a network problem, and there is no remote fix — SDCP is the only
control channel and it is down. It needs a physical power-cycle.

Practical consequences for a client:

- Treat `ECONNREFUSED` / connect timeouts as "the printer is restarting", and
  back off (2s → 60s) rather than retrying tightly.
- A print job survives this: the printer keeps printing even though its
  control plane is gone.
- Check whether the file already landed (cmd 258) before re-uploading, so a lost
  upload response does not leave a duplicate.

## 12. A safe order of operations for a print

1. Wait until the printer reports idle.
2. Wait until the bed is genuinely clear.
3. Upload the gcode in 1 MiB chunks.
4. `sleep 1` → cmd 0 (status check) → `sleep 1` → cmd 128.

That is Elegoo's own sequence (`ElegooLink::loopUpload`: upload, then
`checkResult`, then `print`), and it is worth copying.

### 12.1 Waiting for the bed

Two separate things must both be true before a print starts, and they are
not the same thing:

- **The printer is idle** — no print in progress. Check the status flags,
  and remember that `PREPARING` and `STARTING` count as busy. A print that
  has *finished* is not busy, but the bed may still hold a part.
- **The bed is clear** — nothing left over from the previous print.

A completed print does **not** mean the bed is clear. The part is still
sitting there.

The camera is useful here but must not be trusted on its own: the
brightness of a frame depends on the **light state**, and the light can be
off while the bed is empty (or on while a dark part sits on the plate).
Raw saturation alone will both raise false alarms and miss real ones.

**Fail safe: when the camera cannot tell, ask a human.** The camera
*advises*; a person confirms. Anything that starts a print on a bed that
might not be clear risks damaging the nozzle or the part.

### 12.2 Commands that can wedge the printer

The printer's control stack is not forgiving. A malformed or inappropriate
command can lock up the firmware, and recovery may need a power cycle.

- Stick to the commands this document describes. They are the ones
  Elegoo's own clients use.
- Prefer read-only commands (0 status, 1 attributes, 258 file list,
  324 canvas) when investigating.
- Do not probe the printer with invented commands to see what happens.
- Probe a print start against a **non-existent filename** if you need to
  exercise cmd 128 — the printer answers `Ack 2` (file not found) and no
  print can begin.
- The printer serves **one** websocket client at a time. A second
  connection receives a literal `undefined` rather than protocol frames,
  which looks like a broken client but is really a busy printer.

### 12.3 Do not queue prints

Prints are started one at a time. Queueing a second print while the bed is
still occupied leads to the printer refusing the file or the two prints
fighting over the bed.

Do not start a print merely to test that communication works. Every test
print is filament, time, and a bed that then needs clearing — and the
bed-clear check cannot be validated by a print that is already running.

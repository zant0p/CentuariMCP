# CentauriMCP

A Model Context Protocol (MCP) server for safe, controlled remote operation of the Elegoo Centauri Carbon 3D printer.

## ⚠️ Safety First

This project provides **controlled access** to 3D printer operations with built-in guardrails:

- **Temperature monitoring** - Auto-pause on thermal anomalies
- **Error detection** - Monitors for filament runout, jams, motor failures
- **Rate limiting** - Prevents rapid-fire commands
- **Audit logging** - Every operation logged with timestamp and result
- **Pre-command validation** - Verifies safe state before executing operations

**Never leave a 3D printer unattended while printing.** This tool assists with monitoring and control but does not replace proper safety practices.

## 🎨 CANVAS Multi-Filament Support

**The CC1 DOES expose its CANVAS trays.** SDCP command **324** returns the full tray
list — brand, material, colour hex, nozzle temperature range and whether a spool is
present.

> **Correction:** earlier versions of this README said the CC1 could *not* read slot
> configuration. That is wrong. Cmd 324 works, and it is how the app picks the right
> spool for a requested colour. It was written from the CC2's MQTT behaviour, which
> genuinely is different.

**What works:**
- ✅ CANVAS module connection detection (`AmsConnectStatus`)
- ✅ Reading every tray: brand, material, colour hex, temp range, loaded or not
- ✅ Finding the tray holding a given colour
- ✅ Telling the printer to feed a specific tray (`slot_map` on cmd 128)
- ✅ Filament runout/jam monitoring during prints
- ✅ Video stream for visual monitoring

**What still doesn't work:**
- ❌ Modifying slot configuration via API

### Picking the right spool for a colour

This is the part that is easy to get wrong, so it is worth stating plainly:

- **The gcode only ever names an extruder (`T0`), never a physical spool.** On its
  own the printer picks a spool itself and ignores the colour you asked for.
- **Match by colour hex, not by material name.** A CC1 commonly holds plain "PLA" in
  grey `#898989`, green `#077747` and pink `#F95D77` at once — matching by name picks
  whichever comes first.
- **Then tell the printer, with `slot_map`** in the cmd 128 body:

  ```json
  "slot_map": [ { "t": 0, "tray_id": 2, "canvas_id": 0 } ]
  ```

  Elegoo's own SDK calls this `SlotMapItem { t, trayId, canvasId }`. The CC1 supports
  it, the same way the CC2 does.

**Note:** `status != 0` means a spool is present. Status 1 is "ready"; status 2 is
*also* a loaded spool. Treating only `status == 1` as present silently drops a
loaded spool.

### CANVAS hardware, for context

CANVAS is a four-channel material multiplexer (Happy-Hare style) with a filament
multiplexer at the toolhead. Each spool carries an RFID tag storing brand, material,
**colour**, diameter, weight and nozzle temperatures, so the printer can identify a
spool by itself. The tag format was community-reverse-engineered (Elegoo's published
spec was wrong): <https://github.com/DnG-Crafts/ELG-RFID>.

Full protocol detail: **[docs/SDCP_PROTOCOL.md](docs/SDCP_PROTOCOL.md)**.

## Features

### Phase 1: Read-Only Monitoring ✅
- Real-time printer status (temperatures, position, state)
- Live camera feed access
- File listing (available prints)
- Print history retrieval
- Error code monitoring

### Phase 2: Controlled Operations ✅
- Pause/Resume/Stop print jobs
- Start prints (with validation, and with CANVAS tray selection)
- Fan speed control
- Print speed adjustment
- Chamber light control

### Files
- Upload a gcode to the printer's internal storage (1 MiB chunks)
- List and delete files on the printer
- Read the print task history

### CANVAS
- List the CANVAS trays and what is loaded in each
- Find the tray holding a given colour

## Architecture

- **Protocol**: SDCP v3.0.0 (Smart Device Control Protocol) over WebSocket
- **Connection**: `ws://{printer-ip}:3030/websocket`
- **Language**: Python 3.10+
- **MCP SDK**: `mcp>=1.0.0,<2.0.0` (the 2.x line dropped the low-level
  `Server.list_tools` / `Server.call_tool` decorators this server is built on)

> **Full protocol reference: [docs/SDCP_PROTOCOL.md](docs/SDCP_PROTOCOL.md)** —
> every command, the ack codes, the print/machine status codes, the CANVAS tray
> model, the upload protocol, and the traps that cost hours to rediscover.

## Installation

```bash
# Clone the repository
git clone git@github.com:zant0p/CentuariMCP.git
cd CentuariMCP

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your printer's IP address
```

## Configuration

Create a `.env` file with your settings:

```env
# Printer Configuration
PRINTER_IP=192.168.100.114
PRINTER_PORT=3030
MAINBOARD_ID=60461c580103147000001c0000000000

# Safety Limits
MAX_NOZZLE_TEMP=300
MAX_BED_TEMP=110
COMMAND_RATE_LIMIT_MS=2000

# Logging
LOG_LEVEL=INFO
LOG_FILE=centauri-mcp.log
```

## Usage

### Running the MCP Server

```bash
python -m centauri_mcp.server
```

### Example MCP Client Configuration

```json
{
  "mcpServers": {
    "centauri": {
      "command": "python",
      "args": ["-m", "centauri_mcp.server"],
      "env": {
        "PRINTER_IP": "192.168.100.114"
      }
    }
  }
}
```

### Available Tools

Once connected to an MCP client, you can use:

**Read-only**
- `get_printer_status()` - Current temperatures, position, state, whether the chamber light is on
- `get_printer_attributes()` - Model, firmware version, build volume, capabilities
- `get_camera_status()` - Camera/CANVAS connection state and the video stream URL
- `list_files(path)` - Available print files
- `get_print_history()` - Historical print jobs
- `list_canvas_trays()` - The CANVAS trays and what spool is in each
- `find_tray_by_color(color)` - Which tray holds a given colour

**Operations**
- `start_print(filename, tray_id?, color?, bed_leveling?, bed_type?)` - Start a print job (with validation). Pass `tray_id` or `color` to choose the spool.
- `pause_print()` - Pause current print
- `resume_print()` - Resume paused print
- `emergency_stop(reason)` - Stop print immediately
- `set_fan_speeds(model, auxiliary, box)` - Cooling fan speeds
- `set_print_speed(pct)` - Print speed percentage
- `set_light(on)` - Chamber light on/off
- `upload_file(local_path, remote_name?)` - Upload a gcode to the printer
- `delete_files(filenames)` - Remove files from the printer's storage

## API Reference

- **[docs/SDCP_PROTOCOL.md](docs/SDCP_PROTOCOL.md)** — the reference this server is
  built against: every command, ack codes, status codes, the CANVAS tray model and
  the upload protocol
- [OpenCentauri API Docs](https://docs.opencentauri.cc/software/api/)
- [SDCP Protocol Specification](https://github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0)
- [Elegoo's own SDK](https://github.com/elegooofficial/elegoo-link) — the reference
  implementation for cmd 128 and the CANVAS slot map

## Security

- SSH key-based authentication for Git access
- No credentials stored in repository
- Local network only (no cloud exposure)
- Deploy keys scoped to this repository only

## Development

### Branch Strategy

- `main` - Production-ready code
- `dev` - Active development and testing

### Testing

```bash
# Run tests
pytest tests/

# Test WebSocket connection
python -m centauri_mcp.test_connection
```

## Known Issues

- Protocol field names contain spelling errors (e.g., `CurrenCoord`, `RelaseFilmState`) - must use exact spellings from spec
- Video streaming limited to 1 concurrent connection by default

## License

MIT License - See LICENSE file for details

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request to `dev` branch

---

**Built with ❤️ for safe 3D printer automation**

*Last updated: 2026-07-12*

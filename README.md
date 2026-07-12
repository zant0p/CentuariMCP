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

## Features

### Phase 1: Read-Only Monitoring ✅
- Real-time printer status (temperatures, position, state)
- Live camera feed access
- File listing (available prints)
- Print history retrieval
- Error code monitoring

### Phase 2: Controlled Operations 🚧
- Pause/Resume/Stop print jobs
- Start prints (with validation)
- Fan speed control
- Print speed adjustment

## Architecture

- **Protocol**: SDCP v3.0.0 (Smart Device Control Protocol) over WebSocket
- **Connection**: `ws://{printer-ip}:3030/websocket`
- **Language**: Python 3.10+
- **MCP SDK**: TypeScript MCP SDK reference implementation

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
PRINTER_IP=192.168.1.41
PRINTER_PORT=3030
MAINBOARD_ID=000000000001d354

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
        "PRINTER_IP": "192.168.1.41"
      }
    }
  }
}
```

### Available Tools

Once connected to an MCP client, you can use:

- `get_printer_status()` - Current temperatures, position, state
- `get_camera_snapshot()` - Live camera image
- `list_files()` - Available print files
- `get_print_history()` - Historical print jobs
- `pause_print()` - Pause current print
- `resume_print()` - Resume paused print
- `emergency_stop()` - Stop print immediately
- `start_print(filename)` - Start a print job (with validation)

## API Reference

For detailed SDCP protocol documentation, see:
- [OpenCentauri API Docs](https://docs.opencentauri.cc/software/api/)
- [SDCP Protocol Specification](https://github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0)

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

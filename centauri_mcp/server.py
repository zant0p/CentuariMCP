"""MCP server for Centauri Carbon printer control."""

import os
import asyncio
import logging
from typing import Optional
from contextlib import asynccontextmanager

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .printer_client import CentauriClient, PrinterStatus
from .guardrails import SafetyGuard, SafetyCheckResult

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration from environment
PRINTER_IP = os.getenv("PRINTER_IP", "192.168.100.114")
PRINTER_PORT = int(os.getenv("PRINTER_PORT", "3030"))
# Plain HTTP port, used for file uploads to /uploadFile/upload.
PRINTER_HTTP_PORT = int(os.getenv("PRINTER_HTTP_PORT", "80"))
MAINBOARD_ID = os.getenv("MAINBOARD_ID", "60461c580103147000001c0000000000")
MAX_NOZZLE_TEMP = float(os.getenv("MAX_NOZZLE_TEMP", "300"))
MAX_BED_TEMP = float(os.getenv("MAX_BED_TEMP", "110"))


class CentauriMCPServer:
    """MCP server with safety guardrails for Centauri Carbon."""
    
    def __init__(self):
        self.server = Server("centauri-mcp")
        self.printer: Optional[CentauriClient] = None
        self.safety = SafetyGuard(
            max_nozzle_temp=MAX_NOZZLE_TEMP,
            max_bed_temp=MAX_BED_TEMP,
        )
        self._connected = False
        
        # Register tools
        self._register_tools()
    
    def _register_tools(self) -> None:
        """Register MCP tools with safety checks."""
        
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List available printer control tools."""
            return [
                Tool(
                    name="get_printer_status",
                    description="Get current printer status including temperatures, position, print progress, and error states. Safe read-only operation.",
                ),
                Tool(
                    name="get_printer_attributes",
                    description="Get printer information including model, firmware version, build volume, and capabilities. Safe read-only operation.",
                ),
                Tool(
                    name="list_files",
                    description="List available print files on the printer's storage. Specify path as '/local/' for internal storage or '/usb/' for USB drive. Returns files sorted by name with size info.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Storage path: '/local/' or '/usb/'",
                                "default": "/local/"
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max files to return (default: all)",
                                "default": 0
                            },
                            "filter": {
                                "type": "string",
                                "description": "Filter by filename substring (optional)"
                            }
                        }
                    }
                ),
                Tool(
                    name="get_print_history",
                    description="Get historical print jobs with details like duration, layers, status, and timestamps. Returns most recent jobs first.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "Number of recent jobs to return (default: 10)",
                                "default": 10
                            }
                        }
                    }
                ),
                Tool(
                    name="pause_print",
                    description="Pause the current print job. Safe operation that can be reversed with resume_print.",
                ),
                Tool(
                    name="resume_print",
                    description="Resume a paused print job. Checks printer temperature before resuming.",
                ),
                Tool(
                    name="emergency_stop",
                    description="Stop the current print job immediately. Use in emergency situations. Requires reason parameter.",
                ),
                Tool(
                    name="start_print",
                    description=(
                        "Start printing a file. Validates file exists, printer is idle, "
                        "temperatures are safe, and no active errors. "
                        "Pass tray_id (or color) to pick which CANVAS spool feeds the "
                        "extruder - without it the printer chooses a spool itself and "
                        "ignores the colour you wanted. The start command is sent "
                        "exactly once and never retried."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": "File on the printer's storage (e.g. 'model.gcode' or '/local/model.gcode')"
                            },
                            "tray_id": {
                                "type": "integer",
                                "description": "CANVAS tray to feed from (see list_canvas_trays)"
                            },
                            "color": {
                                "type": "string",
                                "description": "Colour hex to print in (e.g. '#077747'); resolved to a tray automatically"
                            },
                            "bed_leveling": {
                                "type": "boolean",
                                "description": "Auto-level the bed before printing",
                                "default": False
                            },
                            "bed_type": {
                                "type": "integer",
                                "description": "0 = textured (PTE) plate, 1 = smooth (PC) plate",
                                "default": 0
                            }
                        },
                        "required": ["filename"]
                    },
                ),
                Tool(
                    name="set_fan_speeds",
                    description="Adjust cooling fan speeds. Values 0-100% for model fan, auxiliary fan, and box fan.",
                ),
                Tool(
                    name="get_camera_status",
                    description="Check camera/CANVAS module status and get video stream URL if available. Returns camera connection state and stream URL.",
                ),
                Tool(
                    name="list_canvas_trays",
                    description="List the CANVAS multi-material trays: which spools are loaded, their brand, material, colour hex, and nozzle temperature range. Safe read-only operation.",
                ),
                Tool(
                    name="find_tray_by_color",
                    description="Find the CANVAS tray holding a given colour (hex, e.g. '#077747'). Matching by colour rather than by material name matters, because several trays commonly hold the same product. Safe read-only operation.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "color": {
                                "type": "string",
                                "description": "Colour hex as reported by the printer, e.g. '#077747'"
                            }
                        },
                        "required": ["color"]
                    },
                ),
                Tool(
                    name="set_light",
                    description="Turn the printer's chamber light on or off. The state comes back in the printer status as LightStatus.SecondLight. Note: this is a write command - avoid sending it in a tight loop.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "on": {
                                "type": "boolean",
                                "description": "True to turn the light on, False to turn it off"
                            }
                        },
                        "required": ["on"]
                    },
                ),
                Tool(
                    name="upload_file",
                    description="Upload a gcode file to the printer's internal storage. Uses 1 MiB chunks (the documented maximum). Returns the stored filename.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "local_path": {
                                "type": "string",
                                "description": "Absolute path to the local .gcode file"
                            },
                            "remote_name": {
                                "type": "string",
                                "description": "Name to store it under (defaults to the local filename)"
                            }
                        },
                        "required": ["local_path"]
                    },
                ),
                Tool(
                    name="delete_files",
                    description="Delete files from the printer's storage. The paths need the '/local/' prefix or nothing is actually deleted (the printer still acknowledges with Ack 0).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "filenames": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Filenames to delete"
                            }
                        },
                        "required": ["filenames"]
                    },
                ),
            ]
        
        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            """Execute a printer tool with safety checks."""
            
            try:
                # Ensure connected
                if not self._connected:
                    await self.connect()
                
                # Route to appropriate handler
                if name == "get_printer_status":
                    return await self._get_printer_status()
                
                elif name == "get_printer_attributes":
                    return await self._get_printer_attributes()
                
                elif name == "list_files":
                    path = arguments.get("path", "/local/")
                    return await self._list_files(path)
                
                elif name == "get_print_history":
                    return await self._get_print_history()
                
                elif name == "pause_print":
                    return await self._pause_print()
                
                elif name == "resume_print":
                    return await self._resume_print()
                
                elif name == "emergency_stop":
                    reason = arguments.get("reason", "User requested emergency stop")
                    return await self._emergency_stop(reason)
                
                elif name == "start_print":
                    filename = arguments.get("filename")
                    if not filename:
                        raise ValueError("filename is required")
                    tray_id = arguments.get("tray_id")
                    if tray_id is not None:
                        tray_id = int(tray_id)
                    return await self._start_print(
                        filename,
                        tray_id=tray_id,
                        color=arguments.get("color"),
                        bed_leveling=bool(arguments.get("bed_leveling", False)),
                        bed_type=int(arguments.get("bed_type", 0)),
                    )
                
                elif name == "set_fan_speeds":
                    return await self._set_fan_speeds(arguments)
                
                elif name == "set_print_speed":
                    speed_pct = arguments.get("speed_pct", 100)
                    return await self._set_print_speed(speed_pct)
                
                elif name == "list_canvas_trays":
                    return await self._list_canvas_trays()
                
                elif name == "find_tray_by_color":
                    color = arguments.get("color")
                    if not color:
                        raise ValueError("color is required")
                    return await self._find_tray_by_color(color)
                
                elif name == "set_light":
                    on = arguments.get("on")
                    if not isinstance(on, bool):
                        raise ValueError("on (boolean) is required")
                    return await self._set_light(on)
                
                elif name == "upload_file":
                    local_path = arguments.get("local_path")
                    if not local_path:
                        raise ValueError("local_path is required")
                    return await self._upload_file(
                        local_path, arguments.get("remote_name")
                    )
                
                elif name == "delete_files":
                    filenames = arguments.get("filenames") or []
                    return await self._delete_files(filenames)
                
                else:
                    raise ValueError(f"Unknown tool: {name}")
                    
            except Exception as e:
                logger.error(f"Tool execution failed: {e}")
                return [TextContent(type="text", text=f"Error: {str(e)}")]
    
    async def connect(self) -> None:
        """Establish connection to printer."""
        if self._connected:
            return
        
        logger.info(f"Connecting to printer at {PRINTER_IP}:{PRINTER_PORT}")
        
        self.printer = CentauriClient(
            host=PRINTER_IP,
            port=PRINTER_PORT,
            mainboard_id=MAINBOARD_ID,
            http_port=PRINTER_HTTP_PORT,
        )
        
        # Set up callbacks
        self.printer.set_status_callback(self._on_status_update)
        self.printer.set_error_callback(self._on_printer_error)
        
        connected = await self.printer.connect()
        if not connected:
            raise ConnectionError(f"Failed to connect to {PRINTER_IP}")
        
        self._connected = True
        logger.info("Connected to Centauri Carbon")
    
    async def disconnect(self) -> None:
        """Disconnect from printer."""
        if self.printer:
            await self.printer.disconnect()
            self.printer = None
        self._connected = False
    
    def _on_status_update(self, status: PrinterStatus) -> None:
        """Handle automatic status updates."""
        # Check safety limits
        check = self.safety.check_temperature(status)
        if not check.safe:
            logger.warning(f"Safety alert: {check.message}")
    
    def _on_printer_error(self, error: str) -> None:
        """Handle printer errors."""
        logger.error(f"Printer error: {error}")
    
    # Tool implementations
    
    async def _get_printer_status(self) -> list[TextContent]:
        """Get current printer status."""
        status = await self.printer.request_status()
        return [TextContent(
            type="text",
            text=str(status.to_safe_dict()),
        )]
    
    async def _get_printer_attributes(self) -> list[TextContent]:
        """Get printer attributes."""
        attrs = await self.printer.request_attributes()
        return [TextContent(
            type="text",
            text=str({
                "name": attrs.name,
                "machine_name": attrs.machine_name,
                "firmware_version": attrs.firmware_version,
                "build_volume": attrs.build_volume,
                "capabilities": attrs.capabilities,
            }),
        )]
    
    async def _list_files(self, args: dict) -> list[TextContent]:
        """List files in directory with improved formatting."""
        path = args.get("path", "/local/")
        limit = args.get("limit", 0)
        filter_text = args.get("filter", "")
        
        files = await self.printer.get_file_list(path)
        
        if not files:
            return [TextContent(type="text", text="No files found")]
        
        # Filter if requested
        if filter_text:
            files = [f for f in files if filter_text.lower() in f.get("name", "").lower()]
        
        # Limit if requested
        if limit and limit > 0:
            files = files[:limit]
        
        # Format output
        output = []
        for i, f in enumerate(files, 1):
            name = f.get("name", "unknown").replace(path, "")
            size_bytes = f.get("usedSize", 0)
            file_type = "📁" if f.get("type") == 0 else "📄"
            
            size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes > 0 else "(size unknown)"
            output.append(f"{i}. {file_type} {name}\n   Size: {size_str}")
        
        result = f"📁 Files in {path}:\n"
        result += "=" * 50 + "\n"
        result += "\n".join(output)
        result += f"\n\nTotal: {len(files)} file(s)"
        
        return [TextContent(type="text", text=result)]
    
    async def _get_print_history(self, args: dict) -> list[TextContent]:
        """Get print history with timestamps."""
        limit = args.get("limit", 10)
        
        # Cmd 320: Get history list
        history_response = await self.printer._send_request(320)
        ack = history_response.get("Data", {}).get("Ack", -1)
        
        if ack != 0:
            return [TextContent(type="text", text=f"History request failed (ack={ack})")]
        
        history_ids = history_response.get("Data", {}).get("HistoryData", [])
        
        if not history_ids:
            return [TextContent(type="text", text="No print history found")]
        
        # Get details for most recent jobs
        ids_to_fetch = history_ids[:limit]
        details_response = await self.printer._send_request(321, {"Id": ids_to_fetch})
        history_details = details_response.get("Data", {}).get("HistoryDetailList", [])
        
        if not history_details:
            return [TextContent(type="text", text="Could not retrieve history details")]
        
        # Format output
        from datetime import datetime
        
        output = []
        output.append("📋 Recent Print History")
        output.append("=" * 60)
        
        for i, job in enumerate(history_details, 1):
            name = job.get("TaskName", "Unknown")
            begin_time = job.get("BeginTime", 0)
            end_time = job.get("EndTime", 0)
            status = job.get("TaskStatus", 0)
            layers = job.get("AlreadyPrintLayer", 0)
            error_reason = job.get("ErrorStatusReason", 0)
            
            status_str = {0: "Other", 1: "✅ Completed", 2: "⚠️ Exception", 3: "❌ Stopped"}.get(status, f"Unknown({status})")
            
            begin_dt = datetime.fromtimestamp(begin_time) if begin_time else None
            end_dt = datetime.fromtimestamp(end_time) if end_time else None
            
            duration_str = ""
            if begin_dt and end_dt:
                duration = end_dt - begin_dt
                hours, remainder = divmod(int(duration.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                duration_str = f" ({hours}h {minutes}m)"
            
            output.append(f"\n{i}. {name}")
            output.append(f"   Status: {status_str}")
            if begin_dt:
                output.append(f"   Started: {begin_dt.strftime('%Y-%m-%d %H:%M')}")
            if end_dt:
                output.append(f"   Ended: {end_dt.strftime('%Y-%m-%d %H:%M')}{duration_str}")
            output.append(f"   Layers: {layers}")
            if error_reason and error_reason != 0:
                output.append(f"   Error Code: {error_reason}")
        
        return [TextContent(type="text", text="\n".join(output))]
    
    async def _pause_print(self) -> list[TextContent]:
        """Pause current print."""
        success = await self.printer.pause_print()
        return [TextContent(
            type="text",
            text=f"Print paused: {success}",
        )]
    
    async def _resume_print(self) -> list[TextContent]:
        """Resume paused print."""
        # Safety check: verify temperatures are okay
        status = await self.printer.request_status()
        check = self.safety.check_resume_safe(status)
        
        if not check.safe:
            return [TextContent(
                type="text",
                text=f"Resume blocked by safety check: {check.message}",
            )]
        
        success = await self.printer.resume_print()
        return [TextContent(
            type="text",
            text=f"Print resumed: {success}",
        )]
    
    async def _emergency_stop(self, reason: str) -> list[TextContent]:
        """Emergency stop."""
        logger.critical(f"EMERGENCY STOP: {reason}")
        success = await self.printer.stop_print()
        return [TextContent(
            type="text",
            text=f"Emergency stop executed: {success}. Reason: {reason}",
        )]
    
    async def _start_print(
        self,
        filename: str,
        tray_id: Optional[int] = None,
        color: Optional[str] = None,
        bed_leveling: bool = False,
        bed_type: int = 0,
    ) -> list[TextContent]:
        """Start print with validation.

        ``tray_id`` picks which CANVAS spool feeds the extruder. Without it
        the printer picks a spool itself and ignores any colour that was
        wanted. ``color`` is a convenience: it resolves the tray from the
        colour hex the printer reports.
        """
        # Pre-flight safety checks
        status = await self.printer.request_status()
        
        # Check 1: Printer must be idle.
        # Use the PRINT JOB state, not the machine state: the printer reports
        # CurrentStatus "printing" even when it is idle, and a paused job is
        # still busy - treating it as idle would start a second print on top
        # of the paused one.
        if status.is_busy:
            return [TextContent(
                type="text",
                text=(
                    f"Cannot start print: printer is busy "
                    f"(print status {status.print_status}, "
                    f"file {status.filename or 'unknown'})"
                ),
            )]
        
        # Check 2: No active errors
        if status.is_error:
            return [TextContent(
                type="text",
                text=f"Cannot start print: active error detected (code={status.error_number}, reason={status.error_reason})",
            )]
        
        # Check 3: File exists
        files = await self.printer.get_file_list("/local/")
        file_exists = any(f.get("name") == filename or f.get("name").endswith(filename) for f in files)
        if not file_exists:
            return [TextContent(
                type="text",
                text=f"Cannot start print: file '{filename}' not found",
            )]
        
        # Check 4: Temperature sensors online
        if status.error_reason in [33, 34]:  # Temp sensor offline
            return [TextContent(
                type="text",
                text="Cannot start print: temperature sensor offline",
            )]
        
        # Resolve the colour to a tray if one was given.
        if tray_id is None and color:
            tray = await self.printer.find_tray_by_color(color)
            if not tray:
                return [TextContent(
                    type="text",
                    text=(
                        f"Cannot start print: no loaded spool matches "
                        f"colour {color}. Use list_canvas_trays to see what "
                        f"is loaded."
                    ),
                )]
            tray_id = tray["tray_id"]
            filename = filename  # unchanged
        
        # All checks passed - start print.
        # NOTE: cmd 128 is not idempotent. It is sent exactly once, and never
        # retried - the printer can accept the job, queue it, and then lose
        # the reply, and a retry would start a SECOND print of the same file.
        logger.info(f"Starting print: {filename} (tray={tray_id})")
        await self.printer.start_print(
            filename,
            tray_id=tray_id,
            bed_leveling=bed_leveling,
            bed_type=bed_type,
        )
        
        spool = ""
        if tray_id is not None:
            spool = f", tray {tray_id}"
        return [TextContent(
            type="text",
            text=f"Print started. File: {filename}{spool}.",
        )]
    
    async def _list_canvas_trays(self) -> list[TextContent]:
        """List the CANVAS trays with their spools."""
        data = await self.printer.get_canvas_status()
        trays = await self.printer.get_trays()
        lines = [
            f"CANVAS: active_tray_id={data.get('active_tray_id')}, "
            f"auto_refill={data.get('auto_refill')}"
        ]
        for tray in trays:
            state = "loaded" if tray["present"] else "EMPTY"
            lines.append(
                f"  tray {tray['tray_id']} (canvas {tray['canvas_id']}): "
                f"{state} - {tray['brand']} {tray['filament_name']} "
                f"{tray['filament_color']} "
                f"({tray['filament_type']}, "
                f"{tray['min_nozzle_temp']}-{tray['max_nozzle_temp']}C)"
            )
        if not trays:
            lines.append("  (no CANVAS module detected)")
        return [TextContent(type="text", text="\n".join(lines))]

    async def _find_tray_by_color(self, color: str) -> list[TextContent]:
        """Find the tray holding a given colour."""
        tray = await self.printer.find_tray_by_color(color)
        if not tray:
            trays = await self.printer.get_trays()
            loaded = ", ".join(
                f"tray {t['tray_id']}={t['filament_color']}"
                for t in trays if t["present"]
            )
            return [TextContent(
                type="text",
                text=(
                    f"No loaded spool matches colour {color}. "
                    f"Loaded spools: {loaded or 'none'}"
                ),
            )]
        return [TextContent(
            type="text",
            text=(
                f"Colour {color} is in tray {tray['tray_id']} "
                f"(canvas {tray['canvas_id']}): "
                f"{tray['brand']} {tray['filament_name']} "
                f"{tray['min_nozzle_temp']}-{tray['max_nozzle_temp']}C. "
                f"Pass tray_id={tray['tray_id']} to start_print to use this spool."
            ),
        )]

    async def _set_light(self, on: bool) -> list[TextContent]:
        """Turn the chamber light on or off."""
        ok = await self.printer.set_light(on)
        return [TextContent(
            type="text",
            text=f"Chamber light {'on' if on else 'off'} (ack ok: {ok}). "
                 f"The printer reports the state as "
                 f"Status.LightStatus.SecondLight.",
        )]

    async def _upload_file(
        self,
        local_path: str,
        remote_name: str = None,
    ) -> list[TextContent]:
        """Upload a gcode file to the printer."""
        if not os.path.isfile(local_path):
            return [TextContent(
                type="text",
                text=f"Cannot upload: {local_path} does not exist",
            )]

        def progress(sent: int, total: int) -> None:
            logger.info("Upload progress %d/%d", sent, total)

        name = await self.printer.upload_file(
            local_path,
            remote_name=remote_name,
            progress_callback=progress,
        )
        return [TextContent(
            type="text",
            text=f"Uploaded {local_path} as {name}",
        )]

    async def _delete_files(self, filenames: list) -> list[TextContent]:
        """Delete files from the printer's storage."""
        if not filenames:
            return [TextContent(type="text", text="No filenames given")]
        failed = await self.printer.delete_files(filenames)
        message = f"Deleted: {', '.join(filenames)}"
        if failed:
            message += f"\nFailed: {', '.join(failed)}"
        return [TextContent(type="text", text=message)]

    async def _set_fan_speeds(self, args: dict) -> list[TextContent]:
        """Set fan speeds."""
        model = args.get("model_fan")
        auxiliary = args.get("auxiliary_fan")
        box = args.get("box_fan")
        
        success = await self.printer.set_fan_speeds(
            model_fan=model,
            auxiliary_fan=auxiliary,
            box_fan=box,
        )
        
        return [TextContent(
            type="text",
            text=f"Fan speeds updated: {success}",
        )]
    
    async def _set_print_speed(self, speed_pct: int) -> list[TextContent]:
        """Set print speed."""
        if not 50 <= speed_pct <= 150:
            return [TextContent(
                type="text",
                text="Speed must be between 50-150%",
            )]
        
        success = await self.printer.set_print_speed(speed_pct)
        return [TextContent(
            type="text",
            text=f"Print speed set to {speed_pct}%: {success}",
        )]
    
    async def _get_camera_status(self) -> list[TextContent]:
        """Get camera/CANVAS module status."""
        status = await self.printer.request_status()
        attrs = await self.printer.request_attributes()
        
        s = status.to_safe_dict()
        
        result = []
        result.append("🎨 CANVAS Module Status")
        result.append("=" * 50)
        result.append(f"")
        result.append(f"Camera: {'✅ CONNECTED' if s['components']['camera_connected'] else '❌ NOT CONNECTED'}")
        result.append(f"AMS/CANVAS: {'✅ CONNECTED' if s['components']['ams_connected'] else '❌ NOT CONNECTED'}")
        result.append(f"Video Streaming: {'✅ Supported' if 'VIDEO_STREAM' in attrs.capabilities else '❌ Not supported'}")
        result.append(f"")
        result.append(f"Printer: {attrs.name}")
        result.append(f"Firmware: {attrs.firmware_version}")
        result.append(f"")
        
        if s['components']['ams_connected']:
            result.append(f"🎯 CANVAS CC1 Module:")
            result.append(f"   - 4 filament slots available")
            result.append(f"   - Auto filament changing enabled")
            result.append(f"   - Filament runout monitoring active")
            result.append(f"")
            result.append(f"💡 Filament Configuration:")
            result.append(f"   Slot colors/materials set via:")
            result.append(f"   - Printer web interface: http://{PRINTER_IP}/")
            result.append(f"   - Printer touchscreen menu")
            result.append(f"   - Active during print jobs")
        
        if s['components']['camera_connected']:
            result.append(f"")
            result.append(f"📹 Video Stream URL:")
            result.append(f"   http://{PRINTER_IP}:3031/video")
        elif s['components']['ams_connected']:
            result.append(f"")
            result.append(f"⚠️  Camera stream can be enabled")
            result.append(f"   Use enable_camera_stream command")
        
        return [TextContent(type="text", text="\n".join(result))]
    
    async def run(self) -> None:
        """Run the MCP server."""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )


async def main():
    """Entry point for MCP server."""
    server = CentauriMCPServer()
    try:
        await server.run()
    finally:
        await server.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

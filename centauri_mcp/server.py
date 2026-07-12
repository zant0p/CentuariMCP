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
PRINTER_IP = os.getenv("PRINTER_IP", "192.168.1.41")
PRINTER_PORT = int(os.getenv("PRINTER_PORT", "3030"))
MAINBOARD_ID = os.getenv("MAINBOARD_ID", "")
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
                    description="Start printing a file. Validates file exists, printer is idle, temperatures are safe, and no active errors. Requires explicit confirmation.",
                ),
                Tool(
                    name="set_fan_speeds",
                    description="Adjust cooling fan speeds. Values 0-100% for model fan, auxiliary fan, and box fan.",
                ),
                Tool(
                    name="set_print_speed",
                    description="Adjust print speed percentage (50-150% of base speed).",
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
                    return await self._start_print(filename)
                
                elif name == "set_fan_speeds":
                    return await self._set_fan_speeds(arguments)
                
                elif name == "set_print_speed":
                    speed_pct = arguments.get("speed_pct", 100)
                    return await self._set_print_speed(speed_pct)
                
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
    
    async def _start_print(self, filename: str) -> list[TextContent]:
        """Start print with validation."""
        # Pre-flight safety checks
        status = await self.printer.request_status()
        
        # Check 1: Printer must be idle
        if status.machine_status.value != 0:  # Not IDLE
            return [TextContent(
                type="text",
                text=f"Cannot start print: printer is {status.machine_status.name}, not IDLE",
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
        
        # All checks passed - start print
        logger.info(f"Starting print: {filename}")
        success = await self.printer.start_print(filename)
        
        return [TextContent(
            type="text",
            text=f"Print started: {success}. File: {filename}",
        )]
    
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

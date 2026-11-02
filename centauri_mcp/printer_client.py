"""WebSocket client for Centauri Carbon SDCP protocol."""

import asyncio
import json
import time
import uuid
import logging
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)


class MachineStatus(IntEnum):
    """Machine status codes (``CurrentStatus``).

    Elegoo's SDK defines more than the public docs list, and the printer does
    send the undocumented ones - a CC1 was observed reporting 5 (auto levelling)
    while idle. Values outside this enum must not break parsing, so
    ``from_status_dict`` falls back to the raw integer.
    """
    IDLE = 0
    PRINTING = 1
    FILE_TRANSFERRING = 2
    EXPOSURE_TESTING = 3      # resin printers
    PRINTERS_TESTING = 4      # self-check
    AUTO_LEVEL = 5
    RESONANCE_TESTING = 6     # FDM
    OTHERS_BUSY = 7
    FILE_CHECKING = 8
    HOMING = 9
    FEED_OUT = 10
    PID_DETECT = 11


class PrintStatus(IntEnum):
    """Print job status codes."""
    IDLE = 0
    HOMING = 1
    DROPPING = 2
    EXPOSURING = 3
    LIFTING = 4
    PAUSING = 5
    PAUSED = 6
    STOPPING = 7
    STOPPED = 8
    COMPLETE = 9
    FILE_CHECKING = 10
    PRINTERS_CHECKING = 11
    RESUMING = 12
    PRINTING = 13
    # Observed on the CC1 while a job is being set up, before any layer is
    # laid down. The Filename field is EMPTY during these states.
    PREPARING = 16
    STARTING = 20
    AUTO_FEEDING = 23
    FEEDOUT = 24
    FEEDOUT_ABNORMAL = 25
    FEEDOUT_PAUSED = 26

    @classmethod
    def is_busy(cls, value: int) -> bool:
        """True if the printer is doing something and must not be disturbed.

        Anything other than a finished job counts as busy - in particular a
        PAUSED print is still busy, and treating it as idle would let a second
        print start on top of the paused one.
        """
        return value in {
            cls.HOMING, cls.DROPPING, cls.EXPOSURING, cls.LIFTING,
            cls.PAUSING, cls.PAUSED, cls.STOPPING, cls.STOPPED,
            cls.FILE_CHECKING, cls.PRINTERS_CHECKING, cls.RESUMING,
            cls.PREPARING, cls.STARTING, cls.PRINTING,
        }


class StartPrintAck(IntEnum):
    """Ack codes returned by cmd 128 (start print)."""
    OK = 0
    BUSY = 1
    NOT_FOUND = 2
    MD5_FAILED = 3
    FILEIO_FAILED = 4
    INVALID_RESOLUTION = 5
    UNKNOWN_FORMAT = 6
    UNKNOWN_MODEL = 7


class PrintError(IntEnum):
    """Print error codes."""
    NORMAL = 0
    FILE_MD5_FAILED = 1
    FILE_READ_FAILED = 2
    RESOLUTION_MISMATCH = 3
    FORMAT_MISMATCH = 4
    MACHINE_MODEL_MISMATCH = 5


class ExtendedErrorReason(IntEnum):
    """Extended error status reasons."""
    OK = 0
    TEMP_ERROR = 1
    FILAMENT_RUNOUT = 3
    FILAMENT_JAM = 6
    LEVEL_FAILED = 7
    HOME_FAILED_X = 13
    HOME_FAILED_Z = 14
    HOME_FAILED_Y = 23
    HOME_FAILED = 17
    BED_ADHESION_FAILED = 18
    ERROR = 19
    MOVE_ABNORMAL = 20
    FILE_ERROR = 24
    CAMERA_ERROR = 25
    NETWORK_ERROR = 26
    SERVER_CONNECT_FAILED = 27
    DISCONNECT_APP = 28
    UDISK_REMOVE = 12
    NOZZLE_TEMP_SENSOR_OFFLINE = 33
    BED_TEMP_SENSOR_OFFLINE = 34


@dataclass
class PrinterStatus:
    """Current printer status parsed from SDCP messages."""
    machine_status: MachineStatus = MachineStatus.IDLE
    print_status: int = 0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    bed_temp: float = 0.0
    bed_target: float = 0.0
    chamber_temp: float = 0.0
    chamber_target: float = 0.0
    position_x: float = 0.0
    position_y: float = 0.0
    position_z: float = 0.0
    print_speed: int = 100
    current_layer: int = 0
    total_layers: int = 0
    filename: str = ""
    error_number: int = 0
    error_reason: int = 0
    is_error: bool = False
    # True when the chamber light is on.
    light_on: bool = False
    
    # Fan speeds (0-100%)
    model_fan: int = 0
    auxiliary_fan: int = 0
    box_fan: int = 0
    
    # Component status
    motors_connected: bool = True
    camera_connected: bool = False
    ams_connected: bool = False  # CANVAS/AMS module
    
    @property
    def is_busy(self) -> bool:
        """True when the printer is doing something and must not be disturbed.

        Derived from the *print job* state (``PrintInfo.Status``), not from
        ``CurrentStatus`` - the machine reports "printing" even when it is
        idle, and vice versa.
        """
        return PrintStatus.is_busy(self.print_status)

    @classmethod
    def from_status_dict(cls, status: Dict[str, Any]) -> "PrinterStatus":
        """Parse PrinterStatus from SDCP status message."""
        status_data = status.get("Status", {})
        print_info = status_data.get("PrintInfo", {})
        fan_speed = status_data.get("CurrentFanSpeed", {})
        devices_status = status_data.get("DevicesStatus", {})
        
        # Parse position (note: protocol has typo "CurrenCoord")
        coord_str = status_data.get("CurrenCoord", "0,0,0")
        coords = [float(x) for x in coord_str.split(",")]
        
        # Check for errors
        error_num = print_info.get("ErrorNumber", 0)
        error_reason = print_info.get("ErrorStatusReason", 0) if isinstance(print_info, dict) else 0
        is_error = error_num != 0 or error_reason != 0
        
        light_status = status_data.get("LightStatus", {}) or {}

        raw_machine_status = status_data.get("CurrentStatus", [0])[0]
        try:
            machine_status = MachineStatus(raw_machine_status)
        except ValueError:
            # Undocumented value - keep the raw integer rather than failing
            # to parse the whole frame.
            logger.warning(
                "Unknown machine status %s; keeping raw value", raw_machine_status
            )
            machine_status = raw_machine_status

        return cls(
            machine_status=machine_status,
            print_status=print_info.get("Status", 0),
            light_on=int(light_status.get("SecondLight", 0) or 0) == 1,
            nozzle_temp=status_data.get("TempOfNozzle", 0.0),
            nozzle_target=status_data.get("TempTargetNozzle", 0.0),
            bed_temp=status_data.get("TempOfHotbed", 0.0),
            bed_target=status_data.get("TempTargetHotbed", 0.0),
            chamber_temp=status_data.get("TempOfBox", 0.0),
            chamber_target=status_data.get("TempTargetBox", 0.0),
            position_x=coords[0] if len(coords) > 0 else 0.0,
            position_y=coords[1] if len(coords) > 1 else 0.0,
            position_z=coords[2] if len(coords) > 2 else 0.0,
            print_speed=status_data.get("PrintSpeed", 100),
            current_layer=print_info.get("CurrentLayer", 0),
            total_layers=print_info.get("TotalLayer", 0),
            filename=print_info.get("Filename", ""),
            error_number=error_num,
            error_reason=error_reason,
            is_error=is_error,
            model_fan=fan_speed.get("ModelFan", 0),
            auxiliary_fan=fan_speed.get("AuxiliaryFan", 0),
            box_fan=fan_speed.get("BoxFan", 0),
            motors_connected=all([
                devices_status.get("XMotorStatus", 0) == 1,
                devices_status.get("YMotorStatus", 0) == 1,
                devices_status.get("ZMotorStatus", 0) == 1,
                devices_status.get("ExtruderMotorStatus", 0) == 1,
            ]),
            camera_connected=status_data.get("CameraStatus", 0) == 1,
            ams_connected=status_data.get("AmsConnectStatus", 0) == 1,
        )
    
    def to_safe_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MCP tool responses."""
        return {
            "machine_status": self.machine_status.name,
            "print_status": PrintStatus(self.print_status).name if 0 <= self.print_status <= 26 else "UNKNOWN",
            "is_busy": self.is_busy,
            "light_on": self.light_on,
            "nozzle_temp": self.nozzle_temp,
            "nozzle_target": self.nozzle_target,
            "bed_temp": self.bed_temp,
            "bed_target": self.bed_target,
            "chamber_temp": self.chamber_temp,
            "position": {
                "x": self.position_x,
                "y": self.position_y,
                "z": self.position_z,
            },
            "print_speed": self.print_speed,
            "progress": {
                "current_layer": self.current_layer,
                "total_layers": self.total_layers,
                "filename": self.filename,
            },
            "fans": {
                "model": self.model_fan,
                "auxiliary": self.auxiliary_fan,
                "box": self.box_fan,
            },
            "is_error": self.is_error,
            "error_code": self.error_number,
            "error_reason": self.error_reason,
            "components": {
                "motors_connected": self.motors_connected,
                "camera_connected": self.camera_connected,
                "ams_connected": self.ams_connected,  # CANVAS module
            },
        }


@dataclass
class PrinterAttributes:
    """Printer attributes from SDCP."""
    name: str = "Centauri Carbon"
    machine_name: str = "Centauri Carbon"
    brand: str = "Centauri"
    firmware_version: str = "V1.0.0"
    protocol_version: str = "V3.0.0"
    build_volume: str = "300x300x400"
    mainboard_ip: str = ""
    mainboard_id: str = ""
    capabilities: list = field(default_factory=list)
    
    @classmethod
    def from_attributes_dict(cls, attrs: Dict[str, Any]) -> "PrinterAttributes":
        """Parse attributes from SDCP message."""
        return cls(
            name=attrs.get("Name", "Centauri Carbon"),
            machine_name=attrs.get("MachineName", "Centauri Carbon"),
            brand=attrs.get("BrandName", "Centauri"),
            firmware_version=attrs.get("FirmwareVersion", "V1.0.0"),
            protocol_version=attrs.get("ProtocolVersion", "V3.0.0"),
            build_volume=attrs.get("XYZsize", "300x300x400"),
            mainboard_ip=attrs.get("MainboardIP", ""),
            mainboard_id=attrs.get("MainboardID", ""),
            capabilities=attrs.get("Capabilities", []),
        )


class CentauriClient:
    """WebSocket client for Centauri Carbon printer using SDCP protocol."""
    
    def __init__(
        self,
        host: str,
        port: int = 3030,
        mainboard_id: str = "",
        timeout: float = 30.0,
        http_port: int = 80,
    ):
        self.host = host
        self.port = port
        self.mainboard_id = mainboard_id
        self.timeout = timeout
        # The file-upload endpoint is served on the plain HTTP port.
        self.http_port = http_port
        
        self._ws: Optional[Any] = None
        self._connected = False
        self._status: Optional[PrinterStatus] = None
        self._attributes: Optional[PrinterAttributes] = None
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._receive_task: Optional[asyncio.Task] = None
        
        # Callbacks
        self._status_callback: Optional[Callable[[PrinterStatus], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None
    
    @property
    def websocket_url(self) -> str:
        """Get WebSocket connection URL."""
        return f"ws://{self.host}:{self.port}/websocket"
    
    @property
    def connected(self) -> bool:
        """Check if connected to printer."""
        return self._connected and self._ws is not None
    
    @property
    def status(self) -> Optional[PrinterStatus]:
        """Get last known printer status."""
        return self._status
    
    async def connect(self) -> bool:
        """Establish WebSocket connection to printer."""
        try:
            import websockets
            
            logger.info(f"Connecting to {self.websocket_url}")
            self._ws = await websockets.connect(
                self.websocket_url,
                open_timeout=self.timeout,
            )
            self._connected = True
            
            # Start receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())
            
            # Wait a moment for any initial messages
            await asyncio.sleep(0.1)
            
            # Request status and attributes (printer doesn't send automatically)
            logger.info("Requesting initial status and attributes...")
            await self.request_status()
            await asyncio.sleep(0.1)
            await self.request_attributes()
            
            logger.info("Connected to Centauri Carbon")
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._connected = False
            return False
    
    async def disconnect(self) -> None:
        """Close WebSocket connection."""
        self._connected = False
        
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        
        if self._ws:
            await self._ws.close()
            self._ws = None
        
        logger.info("Disconnected from printer")
    
    async def _receive_loop(self) -> None:
        """Listen for incoming messages."""
        try:
            async for message in self._ws:
                await self._handle_message(message)
        except Exception as e:
            logger.error(f"Receive error: {e}")
            if self._error_callback:
                self._error_callback(str(e))
            self._connected = False
    
    async def _handle_message(self, message: str) -> None:
        """Parse and handle incoming WebSocket message."""
        try:
            data = json.loads(message)
            
            # Check topic for routing
            topic = data.get("Topic", "")
            msg_data = data.get("Data", {})
            
            if "sdcp/status/" in topic:
                # Status update
                self._status = PrinterStatus.from_status_dict(data)
                if self._status_callback:
                    self._status_callback(self._status)
                    
            elif "sdcp/attributes/" in topic:
                # Attributes update
                self._attributes = PrinterAttributes.from_attributes_dict(
                    data.get("Attributes", {})
                )
                
            elif "sdcp/response/" in topic:
                # Response to our request - match by RequestID
                request_id = msg_data.get("RequestID", "")
                if request_id in self._pending_requests:
                    future = self._pending_requests.pop(request_id)
                    if not future.done():
                        future.set_result(msg_data)
            
            elif "sdcp/error/" in topic or "sdcp/notice/" in topic:
                # Error or notification
                logger.warning(f"Printer notification: {topic} - {msg_data}")
                if self._error_callback:
                    self._error_callback(f"{topic}: {msg_data}")
                    
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message: {e}")
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    def _build_request(
        self,
        cmd: int,
        data: Dict[str, Any] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build an SDCP request envelope.

        Two fields differ by command, and getting them wrong matters:

        * ``From`` identifies the command source (0 = PC, 1 = WEB_PC, ...).
          Start print (128) uses **1**; everything else uses 0. Elegoo's own
          client sends 1 for cmd 128.
        * ``TimeStamp`` is Unix **seconds** for most commands but
          **milliseconds** for cmd 128.
        """
        request_id = request_id or str(uuid.uuid4())
        is_start_print = cmd == 128
        timestamp = (
            int(time.time() * 1000) if is_start_print else int(time.time())
        )

        return {
            "Id": str(uuid.uuid4()),
            "Data": {
                "Cmd": cmd,
                "Data": data or {},
                "RequestID": request_id,
                "MainboardID": self.mainboard_id,
                "TimeStamp": timestamp,
                "From": 1 if is_start_print else 0,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }

    async def _send_request(self, cmd: int, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command request and wait for response."""
        if not self.connected:
            raise ConnectionError("Not connected to printer")
        
        request_id = str(uuid.uuid4())
        request = self._build_request(cmd, data, request_id)
        
        # Create future to wait for response
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future
        
        # Send request
        await self._ws.send(json.dumps(request))
        logger.debug(f"Sent command {cmd}: {request_id}")
        
        # Wait for response with timeout
        try:
            response = await asyncio.wait_for(future, timeout=self.timeout)
            return response
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise TimeoutError(f"Command {cmd} timed out")
    
    async def request_status(self) -> PrinterStatus:
        """Request current printer status (Cmd: 0)."""
        # Send request and wait for status topic (not response)
        request_id = str(uuid.uuid4())
        timestamp = int(asyncio.get_event_loop().time())
        
        request = {
            "Id": str(uuid.uuid4()),
            "Data": {
                "Cmd": 0,
                "Data": {},
                "RequestID": request_id,
                "MainboardID": self.mainboard_id,
                "TimeStamp": timestamp,
                "From": 0,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }
        
        if not self.connected:
            raise ConnectionError("Not connected to printer")
        
        await self._ws.send(json.dumps(request))
        logger.debug(f"Sent status request: {request_id}")
        
        # Wait for status message (not response)
        for _ in range(10):  # Wait up to 2 seconds
            await asyncio.sleep(0.2)
            if self._status is not None:
                return self._status
        
        raise TimeoutError("No status received from printer")
    
    async def request_attributes(self) -> PrinterAttributes:
        """Request printer attributes (Cmd: 1)."""
        if not self.connected:
            raise ConnectionError("Not connected to printer")
        
        request_id = str(uuid.uuid4())
        timestamp = int(asyncio.get_event_loop().time())
        
        request = {
            "Id": str(uuid.uuid4()),
            "Data": {
                "Cmd": 1,
                "Data": {},
                "RequestID": request_id,
                "MainboardID": self.mainboard_id,
                "TimeStamp": timestamp,
                "From": 0,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }
        
        await self._ws.send(json.dumps(request))
        logger.debug(f"Sent attributes request: {request_id}")
        
        # Wait for attributes message
        for _ in range(10):  # Wait up to 2 seconds
            await asyncio.sleep(0.2)
            if self._attributes is not None:
                return self._attributes
        
        raise TimeoutError("No attributes received from printer")
    
    async def get_file_list(self, path: str = "/local/") -> list:
        """Get list of files in directory (Cmd: 258)."""
        response = await self._send_request(258, {"Url": path})
        ack = response.get("Data", {}).get("Ack", 0)
        if ack != 0:
            raise RuntimeError(f"File list failed with ack: {ack}")
        return response.get("Data", {}).get("FileList", [])
    
    async def start_print(
        self,
        filename: str,
        start_layer: int = 0,
        tray_id: Optional[int] = None,
        canvas_id: int = 0,
        bed_leveling: bool = False,
        bed_type: int = 0,
        timelapse: bool = False,
    ) -> bool:
        """Start printing a file (Cmd: 128).

        Parameters
        ----------
        filename:
            The file **as the printer stored it**, i.e. the full path
            ``/local/<name>``. A bare filename is accepted and expanded.
        tray_id:
            Which CANVAS tray feeds the extruder. This is the important one:
            the gcode only ever names an extruder (``T0``), never a physical
            spool, so without ``slot_map`` the printer picks a spool itself
            and ignores the colour you asked for.
        canvas_id:
            The CANVAS unit the tray belongs to.
        bed_leveling:
            Auto-level the bed before printing.
        bed_type:
            0 = textured (PTE) plate, 1 = smooth (PC) plate.
        timelapse:
            Record a timelapse.

        Returns
        -------
        bool
            True if the printer accepted the job.

        Notes
        -----
        **Send this once.** Cmd 128 is not idempotent: the printer can accept
        the job, queue it, and then lose the reply (its services restart around
        a print), and a retry then starts a *second* print of the same file.
        If you need retry behaviour, verify against the printer's status
        instead of re-sending - see :meth:`is_printing_file`.
        """
        if not filename.startswith("/"):
            filename = f"/local/{filename}"

        data: Dict[str, Any] = {
            "Filename": filename,
            "StartLayer": start_layer,
            "Calibration_switch": 1 if bed_leveling else 0,
            "PrintPlatformType": bed_type,
            "Tlp_Switch": 1 if timelapse else 0,
        }

        if tray_id is not None:
            data["slot_map"] = [
                {"t": 0, "tray_id": tray_id, "canvas_id": canvas_id}
            ]

        response = await self._send_request(128, data)
        ack = response.get("Data", {}).get("Ack", 0)
        if ack != 0:
            raise RuntimeError(
                f"Printer refused to start the print (ack {ack}: "
                f"{StartPrintAck(ack).name if ack in StartPrintAck._value2member_map_ else 'unknown'})"
            )
        return True

    async def is_printing_file(self, filename: str) -> bool:
        """Is the printer running (or preparing) this file?

        Note that the printer reports an EMPTY ``Filename`` while a job is in
        the prepare phase (print states 16 and 20), so a filename match alone
        will miss a print that has in fact just started. This also treats a
        busy printer in a prepare/start state as running.
        """
        status = self.status
        if status is None:
            return False
        if not status.is_busy:
            return False
        if filename and status.filename:
            if filename in status.filename or status.filename.endswith(filename):
                return True
        return status.print_status in (
            PrintStatus.PREPARING,
            PrintStatus.STARTING,
            PrintStatus.PRINTING,
        )
    
    async def pause_print(self) -> bool:
        """Pause current print (Cmd: 129)."""
        response = await self._send_request(129)
        ack = response.get("Data", {}).get("Ack", 0)
        return ack == 0
    
    async def resume_print(self) -> bool:
        """Resume paused print (Cmd: 131)."""
        response = await self._send_request(131)
        ack = response.get("Data", {}).get("Ack", 0)
        return ack == 0
    
    async def stop_print(self) -> bool:
        """Stop current print (Cmd: 130)."""
        response = await self._send_request(130)
        ack = response.get("Data", {}).get("Ack", 0)
        return ack == 0
    
    async def get_canvas_status(self) -> Dict[str, Any]:
        """Read the CANVAS multi-material module (Cmd: 324).

        Returns the raw ``canvas_list`` structure. Each tray entry carries
        ``tray_id``, ``brand``, ``filament_type``, ``filament_name``,
        ``filament_color`` (hex), ``min_nozzle_temp``, ``max_nozzle_temp`` and
        ``status``.

        NOTE: ``status != 0`` means a spool is present. Status 1 is "ready";
        status 2 is also a loaded spool. Treating only ``status == 1`` as
        present silently drops a loaded spool.
        """
        response = await self._send_request(324)
        ack = response.get("Data", {}).get("Ack", 0)
        if ack != 0:
            raise RuntimeError(f"Canvas status failed with ack: {ack}")
        return response.get("Data", {})

    async def get_trays(self) -> list:
        """The CANVAS trays, flattened, each with a normalised ``present`` flag."""
        data = await self.get_canvas_status()
        trays = []
        for canvas in data.get("canvas_list", []):
            if not canvas.get("connected"):
                continue
            for tray in canvas.get("tray_list", []):
                trays.append({
                    "tray_id": tray.get("tray_id"),
                    "canvas_id": canvas.get("canvas_id", 0),
                    "present": tray.get("status", 0) != 0,
                    "brand": tray.get("brand", ""),
                    "filament_type": tray.get("filament_type", ""),
                    "filament_name": tray.get("filament_name", ""),
                    "filament_color": tray.get("filament_color", ""),
                    "min_nozzle_temp": tray.get("min_nozzle_temp"),
                    "max_nozzle_temp": tray.get("max_nozzle_temp"),
                    "status": tray.get("status"),
                })
        return trays

    async def find_tray_by_color(self, color_hex: str) -> Optional[dict]:
        """Find the loaded CANVAS tray holding a given colour.

        Match on the **colour hex** rather than the filament name: several
        trays commonly hold the same product (plain "PLA" in grey, green and
        pink at once), and matching by name picks whichever comes first.
        """
        want = (color_hex or "").lower()
        if not want:
            return None
        trays = await self.get_trays()
        loaded = [t for t in trays if t["present"]]
        for tray in loaded:
            if (tray["filament_color"] or "").lower() == want:
                return tray
        return None

    async def set_light(self, on: bool) -> bool:
        """Turn the chamber light on or off (Cmd: 403).

        The printer reports the state back as
        ``Status.LightStatus.SecondLight`` (0 = off, 1 = on).

        Note that Elegoo's own SDK has the light variant of cmd 403 commented
        out, and a badly-formed write command can wedge the printer's control
        stack. Send it deliberately, not in a tight loop.
        """
        response = await self._send_request(
            403, {"LightStatus": {"SecondLight": 1 if on else 0}}
        )
        ack = response.get("Data", {}).get("Ack", 0)
        return ack == 0

    async def delete_files(self, filenames: list) -> list:
        """Delete files from the printer's storage (Cmd: 259).

        The paths must include the ``/local/`` prefix, otherwise the printer
        acknowledges with Ack 0 but deletes nothing.
        """
        file_list = [
            f if f.startswith("/") else f"/local/{f}" for f in filenames
        ]
        response = await self._send_request(
            259, {"FileList": file_list, "FolderList": []}
        )
        data = response.get("Data", {})
        ack = data.get("Ack", 0)
        if ack != 0:
            raise RuntimeError(f"Delete failed with ack: {ack}")
        return data.get("ErrData", [])

    async def upload_file(
        self,
        local_path: str,
        remote_name: Optional[str] = None,
        chunk_size: int = 1024 * 1024,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """Upload a gcode file to the printer's internal storage.

        ``POST /uploadFile/upload`` as multipart/form-data, one 1 MiB chunk per
        request. 1 MiB is the documented per-chunk maximum; larger chunks are
        a plausible cause of the printer's upload handler wedging.

        Form fields: ``Check=1``, ``S-File-MD5`` (whole-file md5, sent on
        every chunk), ``Offset``, ``Uuid`` (one per transfer, shared by all
        its chunks), ``TotalSize``, and ``File``.

        The uuid must be a bare hex string - ``uuid4().hex``, no dashes.
        """
        import hashlib
        import os
        import requests

        remote_name = remote_name or os.path.basename(local_path)
        size = os.path.getsize(local_path)
        md5 = hashlib.md5()
        with open(local_path, "rb") as fh:
            for block in iter(lambda: fh.read(chunk_size), b""):
                md5.update(block)
        file_md5 = md5.hexdigest()

        transfer_id = uuid.uuid4().hex
        url = f"http://{self.host}:{self.http_port}/uploadFile/upload"

        offset = 0
        with open(local_path, "rb") as fh:
            while offset < size:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                response = requests.post(
                    url,
                    data={
                        "TotalSize": str(size),
                        "Uuid": transfer_id,
                        "Offset": str(offset),
                        "Check": "1",
                        "S-File-MD5": file_md5,
                    },
                    files={"File": (remote_name, chunk, "application/octet-stream")},
                    timeout=120,
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Upload failed with HTTP {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                try:
                    result = response.json()
                except ValueError as exc:
                    raise RuntimeError(
                        f"Upload returned non-JSON: {response.text[:200]}"
                    ) from exc
                if result.get("code") != "000000":
                    raise RuntimeError(
                        f"Upload rejected (code {result.get('code')}): "
                        f"{result.get('messages')}"
                    )
                offset += len(chunk)
                if progress_callback:
                    progress_callback(offset, size)

        return remote_name

    async def set_print_speed(self, speed_pct: int) -> bool:
        """Set print speed percentage (Cmd: 403)."""
        if not 0 <= speed_pct <= 100:
            raise ValueError("Speed must be 0-100%")
        
        response = await self._send_request(403, {
            "PrintSpeedPct": speed_pct,
        })
        ack = response.get("Data", {}).get("Ack", 0)
        return ack == 0
    
    async def set_fan_speeds(
        self,
        model_fan: int = None,
        auxiliary_fan: int = None,
        box_fan: int = None,
    ) -> bool:
        """Set fan speeds (Cmd: 403)."""
        data = {}
        if model_fan is not None:
            data["ModelFan"] = model_fan
        if auxiliary_fan is not None:
            data["AuxiliaryFan"] = auxiliary_fan
        if box_fan is not None:
            data["BoxFan"] = box_fan
        
        response = await self._send_request(403, {
            "TargetFanSpeed": data,
        })
        ack = response.get("Data", {}).get("Ack", 0)
        return ack == 0
    
    async def enable_camera_stream(self, enable: bool = True) -> dict:
        """Enable/disable camera video stream (Cmd: 386)."""
        response = await self._send_request(386, {"Enable": 1 if enable else 0})
        ack = response.get("Data", {}).get("Ack", 0)
        video_url = response.get("Data", {}).get("VideoUrl", "") if ack == 0 else ""
        return {"success": ack == 0, "video_url": video_url}
    
    async def ping(self) -> bool:
        """Send heartbeat ping."""
        if not self.connected:
            return False
        
        await self._ws.send("ping")
        # Expect "pong" response (handled in receive loop)
        return True
    
    def set_status_callback(self, callback: Callable[[PrinterStatus], None]) -> None:
        """Set callback for status updates."""
        self._status_callback = callback
    
    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for errors."""
        self._error_callback = callback

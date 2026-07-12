"""WebSocket client for Centauri Carbon SDCP protocol."""

import asyncio
import json
import uuid
import logging
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)


class MachineStatus(IntEnum):
    """Machine status codes from SDCP protocol."""
    IDLE = 0
    PRINTING = 1
    FILE_TRANSFERRING = 2
    CALIBRATING = 3
    DEVICE_TESTING = 4


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
    
    # Fan speeds (0-100%)
    model_fan: int = 0
    auxiliary_fan: int = 0
    box_fan: int = 0
    
    # Component status
    motors_connected: bool = True
    camera_connected: bool = False
    
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
        
        return cls(
            machine_status=MachineStatus(status_data.get("CurrentStatus", [0])[0]),
            print_status=print_info.get("Status", 0),
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
        )
    
    def to_safe_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MCP tool responses."""
        return {
            "machine_status": self.machine_status.name,
            "print_status": PrintStatus(self.print_status).name if 0 <= self.print_status <= 10 else "UNKNOWN",
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
    ):
        self.host = host
        self.port = port
        self.mainboard_id = mainboard_id
        self.timeout = timeout
        
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
    
    async def _send_request(self, cmd: int, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command request and wait for response."""
        if not self.connected:
            raise ConnectionError("Not connected to printer")
        
        request_id = str(uuid.uuid4())
        timestamp = int(asyncio.get_event_loop().time())
        
        request = {
            "Id": str(uuid.uuid4()),
            "Data": {
                "Cmd": cmd,
                "Data": data or {},
                "RequestID": request_id,
                "MainboardID": self.mainboard_id,
                "TimeStamp": timestamp,
                "From": 0,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }
        
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
    
    async def start_print(self, filename: str, start_layer: int = 0) -> bool:
        """Start printing a file (Cmd: 128)."""
        response = await self._send_request(128, {
            "Filename": filename,
            "StartLayer": start_layer,
        })
        ack = response.get("Data", {}).get("Ack", 0)
        return ack == 0
    
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

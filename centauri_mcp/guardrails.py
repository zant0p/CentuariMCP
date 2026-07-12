"""Safety guardrails for 3D printer operations."""

import logging
from dataclasses import dataclass
from typing import Optional

from .printer_client import PrinterStatus, MachineStatus, ExtendedErrorReason

logger = logging.getLogger(__name__)


@dataclass
class SafetyCheckResult:
    """Result of a safety check."""
    safe: bool
    message: str
    check_type: str


class SafetyGuard:
    """Safety guardrails for Centauri Carbon operations."""
    
    def __init__(
        self,
        max_nozzle_temp: float = 300.0,
        max_bed_temp: float = 110.0,
        max_chamber_temp: float = 60.0,
        rate_limit_ms: int = 2000,
    ):
        self.max_nozzle_temp = max_nozzle_temp
        self.max_bed_temp = max_bed_temp
        self.max_chamber_temp = max_chamber_temp
        self.rate_limit_ms = rate_limit_ms
        
        # Track last command time for rate limiting
        self._last_command_time: float = 0
    
    def check_temperature(self, status: PrinterStatus) -> SafetyCheckResult:
        """Check if temperatures are within safe limits."""
        
        # Check nozzle temperature
        if status.nozzle_temp > self.max_nozzle_temp:
            return SafetyCheckResult(
                safe=False,
                message=f"Nozzle temperature {status.nozzle_temp}°C exceeds maximum {self.max_nozzle_temp}°C",
                check_type="nozzle_overtemp",
            )
        
        # Check bed temperature
        if status.bed_temp > self.max_bed_temp:
            return SafetyCheckResult(
                safe=False,
                message=f"Bed temperature {status.bed_temp}°C exceeds maximum {self.max_bed_temp}°C",
                check_type="bed_overtemp",
            )
        
        # Check chamber temperature
        if status.chamber_temp > self.max_chamber_temp:
            return SafetyCheckResult(
                safe=False,
                message=f"Chamber temperature {status.chamber_temp}°C exceeds maximum {self.max_chamber_temp}°C",
                check_type="chamber_overtemp",
            )
        
        # Check for temperature sensor failures
        if status.error_reason in [
            ExtendedErrorReason.NOZZLE_TEMP_SENSOR_OFFLINE.value,
            ExtendedErrorReason.BED_TEMP_SENSOR_OFFLINE.value,
        ]:
            return SafetyCheckResult(
                safe=False,
                message="Temperature sensor offline - cannot monitor safely",
                check_type="sensor_offline",
            )
        
        # Check for thermal error
        if status.error_reason == ExtendedErrorReason.TEMP_ERROR.value:
            return SafetyCheckResult(
                safe=False,
                message="Thermal error detected - printer has detected over-temperature condition",
                check_type="thermal_error",
            )
        
        return SafetyCheckResult(
            safe=True,
            message="All temperatures within safe limits",
            check_type="temperature",
        )
    
    def check_motion_system(self, status: PrinterStatus) -> SafetyCheckResult:
        """Check if motion system is healthy."""
        
        if not status.motors_connected:
            return SafetyCheckResult(
                safe=False,
                message="One or more stepper motors disconnected",
                check_type="motor_disconnected",
            )
        
        # Check for homing failures
        if status.error_reason in [
            ExtendedErrorReason.HOME_FAILED_X.value,
            ExtendedErrorReason.HOME_FAILED_Y.value,
            ExtendedErrorReason.HOME_FAILED_Z.value,
            ExtendedErrorReason.HOME_FAILED.value,
        ]:
            return SafetyCheckResult(
                safe=False,
                message=f"Homing failure detected (reason={status.error_reason})",
                check_type="homing_failure",
            )
        
        # Check for movement abnormalities
        if status.error_reason == ExtendedErrorReason.MOVE_ABNORMAL.value:
            return SafetyCheckResult(
                safe=False,
                message="Abnormal movement detected - possible obstruction or motor issue",
                check_type="move_abnormal",
            )
        
        return SafetyCheckResult(
            safe=True,
            message="Motion system healthy",
            check_type="motion",
        )
    
    def check_print_health(self, status: PrinterStatus) -> SafetyCheckResult:
        """Check print job health."""
        
        # Check for filament issues
        if status.error_reason == ExtendedErrorReason.FILAMENT_RUNOUT.value:
            return SafetyCheckResult(
                safe=False,
                message="Filament runout detected",
                check_type="filament_runout",
            )
        
        if status.error_reason == ExtendedErrorReason.FILAMENT_JAM.value:
            return SafetyCheckResult(
                safe=False,
                message="Filament jam detected",
                check_type="filament_jam",
            )
        
        # Check for bed adhesion failure
        if status.error_reason == ExtendedErrorReason.BED_ADHESION_FAILED.value:
            return SafetyCheckResult(
                safe=False,
                message="Print detachment from bed detected",
                check_type="bed_adhesion_failed",
            )
        
        # Check for general errors
        if status.is_error:
            return SafetyCheckResult(
                safe=False,
                message=f"Print error detected (code={status.error_number}, reason={status.error_reason})",
                check_type="print_error",
            )
        
        return SafetyCheckResult(
            safe=True,
            message="Print job healthy",
            check_type="print_health",
        )
    
    def check_resume_safe(self, status: PrinterStatus) -> SafetyCheckResult:
        """Check if it's safe to resume a paused print."""
        
        # All safety checks must pass
        temp_check = self.check_temperature(status)
        if not temp_check.safe:
            return temp_check
        
        motion_check = self.check_motion_system(status)
        if not motion_check.safe:
            return motion_check
        
        # Printer must be in PAUSED state
        from .printer_client import PrintStatus
        if status.print_status != PrintStatus.PAUSED.value:
            return SafetyCheckResult(
                safe=False,
                message=f"Cannot resume: printer is not paused (status={status.print_status})",
                check_type="not_paused",
            )
        
        return SafetyCheckResult(
            safe=True,
            message="Safe to resume print",
            check_type="resume_check",
        )
    
    def check_start_safe(self, status: PrinterStatus) -> SafetyCheckResult:
        """Check if it's safe to start a new print."""
        
        # Temperature check
        temp_check = self.check_temperature(status)
        if not temp_check.safe:
            return temp_check
        
        # Motion system check
        motion_check = self.check_motion_system(status)
        if not motion_check.safe:
            return motion_check
        
        # Printer must be IDLE
        if status.machine_status != MachineStatus.IDLE:
            return SafetyCheckResult(
                safe=False,
                message=f"Cannot start: printer is {status.machine_status.name}, not IDLE",
                check_type="not_idle",
            )
        
        # No active print job
        from .printer_client import PrintStatus
        if status.print_status not in [PrintStatus.IDLE.value, PrintStatus.COMPLETE.value]:
            return SafetyCheckResult(
                safe=False,
                message=f"Cannot start: existing print job in state {status.print_status}",
                check_type="job_exists",
            )
        
        return SafetyCheckResult(
            safe=True,
            message="Safe to start print",
            check_type="start_check",
        )
    
    def check_rate_limit(self, current_time: float) -> SafetyCheckResult:
        """Check if command rate limit allows execution."""
        elapsed_ms = (current_time - self._last_command_time) * 1000
        
        if elapsed_ms < self.rate_limit_ms:
            remaining_ms = int(self.rate_limit_ms - elapsed_ms)
            return SafetyCheckResult(
                safe=False,
                message=f"Rate limit: wait {remaining_ms}ms before next command",
                check_type="rate_limit",
            )
        
        self._last_command_time = current_time
        return SafetyCheckResult(
            safe=True,
            message="Rate limit OK",
            check_type="rate_limit",
        )
    
    def get_all_safety_status(self, status: PrinterStatus) -> dict:
        """Get comprehensive safety status report."""
        temp = self.check_temperature(status)
        motion = self.check_motion_system(status)
        health = self.check_print_health(status)
        
        return {
            "overall_safe": all([temp.safe, motion.safe, health.safe]),
            "temperature": {
                "safe": temp.safe,
                "message": temp.message,
                "nozzle": status.nozzle_temp,
                "bed": status.bed_temp,
                "chamber": status.chamber_temp,
            },
            "motion": {
                "safe": motion.safe,
                "message": motion.message,
                "motors_connected": status.motors_connected,
            },
            "print_health": {
                "safe": health.safe,
                "message": health.message,
                "error_code": status.error_number,
                "error_reason": status.error_reason,
            },
        }

"""`doctor`: run every preflight check and fold the results into one outcome.

A composite command: its only leaf import is validate's schema check.
"""

from chart_manager.commands.doctor.models import DoctorReport

__all__ = ["DoctorReport"]

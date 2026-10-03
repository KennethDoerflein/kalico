# Heat soak manager with interruptible countdown and skip support for K2 Plus / Kalico
#
# Copyright (C) 2026 Kenneth Doerflein
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging


def _klog(msg, *args, level=logging.info):
    level("heat_soak: " + msg, *args)


class HeatSoak:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.soak_active = False
        self.skip_requested = False
        self.time_remaining = 0
        self.total_time = 0

        # Register G-code commands
        for cmd, func, desc in (
            ("HEAT_SOAK", self.cmd_HEAT_SOAK, "Perform heat soak with countdown and skip capability"),
            ("SOAK_WAIT", self.cmd_HEAT_SOAK, "Alias for HEAT_SOAK"),
            ("_HEAT_SOAK_SKIP", self.cmd_HEAT_SOAK_SKIP, "Internal command to skip the active heat soak countdown"),
            ("SOAK_INTERRUPT", self.cmd_HEAT_SOAK_SKIP, "Interrupt and skip the active heat soak countdown"),
            ("SOAK_SKIP", self.cmd_HEAT_SOAK_SKIP, "Skip the active heat soak countdown"),
        ):
            is_reg = (
                self.gcode.is_command_registered(cmd)
                if hasattr(self.gcode, "is_command_registered")
                else cmd in getattr(self.gcode, "ready_gcode_handlers", {})
            )
            if not is_reg:
                self.gcode.register_command(cmd, func, desc=desc)

        # Register webhook for web UI / Fluidd / Mainsail / API access
        webhooks = self.printer.lookup_object("webhooks")
        webhooks.register_endpoint("skip_soak", self._handle_webhook_skip_soak)

    def _send_status(self, msg):
        display_status = self.printer.lookup_object("display_status", None)
        if display_status is not None:
            display_status.message = msg
        try:
            self.gcode.run_script_from_command(
                f'STATUS_MSG PREFIX="[START_PRINT]:" MSG="{msg}"'
            )
        except Exception:
            self.gcode.respond_info(f"[START_PRINT]: {msg}")

    def cmd_HEAT_SOAK(self, gcmd):
        if self.soak_active:
            raise gcmd.error("Heat soak is already in progress")

        soak_minutes = gcmd.get_float("SOAK_TIME", None)
        if soak_minutes is None:
            soak_minutes = gcmd.get_float("MINUTES", None)
        if soak_minutes is None:
            seconds = gcmd.get_float("SECONDS", 0.0)
            total_seconds = int(seconds)
        else:
            total_seconds = int(soak_minutes * 60)

        if total_seconds <= 0:
            return

        self.soak_active = True
        self.skip_requested = False
        self.total_time = total_seconds
        self.time_remaining = total_seconds

        start_time = self.reactor.monotonic()
        deadline = start_time + total_seconds
        last_reported_sec = None

        gcode = self.gcode
        counter = gcode.get_interrupt_counter()
        vsd = self.printer.lookup_object("virtual_sdcard", None)
        is_sd_print = vsd is not None and vsd.is_active()

        def condition(eventtime):
            nonlocal last_reported_sec
            if self.skip_requested:
                return False
            if gcode.get_interrupt_counter() != counter:
                self.skip_requested = True
                return False
            if is_sd_print and (vsd.must_pause_work or not vsd.is_active()):
                return False

            remaining = int(deadline - eventtime)
            self.time_remaining = max(0, remaining)
            if remaining <= 0:
                return False

            elapsed = int(eventtime - start_time)
            if last_reported_sec is None or (elapsed - last_reported_sec >= 60):
                last_reported_sec = elapsed
                mins = remaining // 60
                secs = remaining % 60
                self._send_status(f"Soaking: {mins} min {secs} sec left")

            return True

        try:
            self.printer.wait_while(condition, error_on_cancel=False, interval=0.5)
        finally:
            self.soak_active = False

        if self.skip_requested or gcode.get_interrupt_counter() != counter:
            self.skip_requested = True
            self._send_status("Heat soak skipped!")
        elif is_sd_print and (vsd.must_pause_work or not vsd.is_active()):
            self._send_status("Heat soak cancelled")
        else:
            self._send_status("Heat soak complete.")

    def cmd_HEAT_SOAK_SKIP(self, gcmd):
        if self.soak_active:
            self.skip_requested = True
            self.gcode.increment_interrupt_counter()
            gcmd.respond_info("Heat soak skip requested - continuing print.")
        else:
            gcmd.respond_info("No heat soak currently active.")

    def _handle_webhook_skip_soak(self, web_request):
        if self.soak_active:
            self.skip_requested = True
            self.gcode.increment_interrupt_counter()
            web_request.send(
                {"result": "success", "message": "Heat soak skip requested"}
            )
        else:
            web_request.send(
                {"result": "ignored", "message": "No heat soak currently active"}
            )

    def get_status(self, eventtime):
        return {
            "soak_active": self.soak_active,
            "skip_requested": self.skip_requested,
            "total_time": self.total_time,
            "time_remaining": self.time_remaining,
        }


def load_config(config):
    return HeatSoak(config)

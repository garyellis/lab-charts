"""Process logging stays readable for humans and separate from stdout."""

from __future__ import annotations

import io
import json
import logging
import re

from rich.console import Console

from chart_manager.plumbing.logger import setup_logging


def test_setup_logging_renders_levels_and_literal_messages_to_stderr() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, no_color=True, width=200)
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    try:
        setup_logging("DEBUG", console=console)
        logger = logging.getLogger("chart-manager-test")

        logger.debug("chart path: charts/alloy")
        logger.info("starting [literal] work")
        logger.error("lookup failed")
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)

    rendered = output.getvalue()
    assert re.search(
        r"\d{4}-\d{2}-\d{2}T.*Z \| DEBUG    \| "
        r"chart-manager-test\.test_setup_logging_renders_levels_and_literal_messages_to_stderr:"
        r"\d+ \| chart path: charts/alloy",
        rendered,
    )
    assert " | INFO     | " in rendered
    assert " | starting [literal] work" in rendered
    assert " | ERROR    | " in rendered
    assert " | lookup failed" in rendered


def test_setup_logging_filters_debug_at_info_level() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, no_color=True, width=200)
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    try:
        setup_logging("INFO", console=console)
        logger = logging.getLogger("chart-manager-test")

        logger.debug("hidden detail")
        logger.info("visible progress")
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)

    assert "hidden detail" not in output.getvalue()
    assert "visible progress" in output.getvalue()


def test_setup_logging_quiets_dependency_chatter_at_info_level() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, no_color=True, width=200)
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    azure = logging.getLogger("azure")
    previous_azure_level = azure.level
    try:
        setup_logging("INFO", console=console)
        sdk_logger = logging.getLogger("azure.cosmos._cosmos_http_logging_policy")

        sdk_logger.info("Request URL: https://example/")
        sdk_logger.warning("retrying after throttle")
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)
        azure.setLevel(previous_azure_level)

    assert "Request URL" not in output.getvalue()
    assert "retrying after throttle" in output.getvalue()


def test_setup_logging_reopens_dependency_chatter_at_debug_level() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, no_color=True, width=200)
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    azure = logging.getLogger("azure")
    previous_azure_level = azure.level
    try:
        setup_logging("INFO", console=console)
        setup_logging("DEBUG", console=console)
        logging.getLogger("azure.cosmos").info("Request URL: https://example/")
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)
        azure.setLevel(previous_azure_level)

    assert "Request URL" in output.getvalue()


def test_setup_logging_renders_json_with_execution_fields() -> None:
    output = io.StringIO()
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    try:
        setup_logging("INFO", fmt="json", stream=output)
        logging.getLogger("chart_manager.example").info("rendering %s", "alloy")
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)

    entry = json.loads(output.getvalue())
    assert entry["timestamp"].endswith("Z")
    assert entry["level"] == "INFO"
    assert entry["logger"] == "chart_manager.example"
    assert entry["module"] == "test_logger"
    assert entry["function"] == "test_setup_logging_renders_json_with_execution_fields"
    assert isinstance(entry["line"], int)
    assert entry["message"] == "rendering alloy"


def test_setup_logging_json_includes_exception_details() -> None:
    output = io.StringIO()
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    try:
        setup_logging("ERROR", fmt="json", stream=output)
        try:
            raise RuntimeError("helm failed")
        except RuntimeError:
            logging.getLogger("chart_manager.example").exception("upgrade failed")
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)

    entry = json.loads(output.getvalue())
    assert entry["error"] == "helm failed"
    assert entry["error_type"] == "RuntimeError"
    assert "RuntimeError: helm failed" in entry["stack_trace"]

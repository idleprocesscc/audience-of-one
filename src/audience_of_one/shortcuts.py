"""Stable macOS launcher and importable keyboard-shortcut workflow."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

SHORTCUT_NAME = "Audience of One Toggle"


def launcher_path() -> Path:
    return Path.home() / ".local" / "bin" / "audience-of-one-toggle"


def artifact_path() -> Path:
    return (
        Path.home() / ".local" / "share" / "audience-of-one"
        / f"{SHORTCUT_NAME}.workflow"
    )


def _launcher(python: Path, config: Path, device: str) -> str:
    command = [
        str(python), "-m", "audience_of_one", "--config", str(config),
        "toggle", "--device", device,
    ]
    return "#!/bin/sh\nexec " + " ".join(shlex.quote(part) for part in command) + "\n"


def _workflow(command: str) -> dict[str, Any]:
    return {
        "AMApplicationBuild": "523",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{"action": {
            "AMAccepts": {"Container": "List", "Optional": True,
                          "Types": ["com.apple.cocoa.string"]},
            "AMActionVersion": "2.0.3",
            "AMApplication": ["Automator"],
            "AMBundleIdentifier": "com.apple.RunShellScript",
            "AMCategory": ["AMCategoryUtilities"],
            "AMIconName": "Automator",
            "AMKeywords": ["Shell", "Script"],
            "AMName": "Run Shell Script",
            "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
            "AMRequiredResources": [],
            "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
            "ActionName": "Run Shell Script",
            "ActionParameters": {
                "COMMAND_STRING": command,
                "CheckedForUserDefaultShell": True,
                "inputMethod": 0,
                "shell": "/bin/sh",
                "source": "",
            },
            "BundleIdentifier": "com.apple.RunShellScript",
            "CFBundleVersion": "2.0.3",
            "CanShowSelectedItemsWhenRun": False,
            "CanShowWhenRun": True,
            "Category": ["AMCategoryUtilities"],
            "Class Name": "RunShellScriptAction",
            "InputUUID": str(uuid.uuid4()).upper(),
            "Keywords": ["Shell", "Script"],
            "OutputUUID": str(uuid.uuid4()).upper(),
            "UUID": str(uuid.uuid4()).upper(),
            "UnlocalizedApplications": ["Automator"],
        }}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleID": "com.apple.finder",
            "applicationBundleIDsByPath": {},
            "applicationPath": "/System/Library/CoreServices/Finder.app",
            "inputTypeIdentifier": "com.apple.Automator.nothing",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15,
            "processesInput": 0,
            "serviceApplicationGroupName": "General",
            "serviceInputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
        },
    }


def install(config: Path, device: str, *, open_import: bool = True) -> dict[str, Any]:
    launcher = launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    # Preserve the virtual-environment entrypoint. Resolving its symlink reaches the
    # base uv/python installation, where audience_of_one is not installed.
    launcher.write_text(_launcher(Path(sys.executable).absolute(), config.resolve(), device),
                        encoding="utf-8")
    launcher.chmod(0o700)

    workflow = artifact_path()
    contents = workflow / "Contents"
    contents.mkdir(parents=True, exist_ok=True, mode=0o755)
    with (contents / "document.wflow").open("wb") as handle:
        plistlib.dump(_workflow(shlex.quote(str(launcher))), handle, fmt=plistlib.FMT_XML)
    opened = False
    if open_import and sys.platform == "darwin":
        result = subprocess.run(
            ["open", "-a", "Shortcuts", str(workflow)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        opened = result.returncode == 0
    return {
        "installed": True,
        "launcher": str(launcher),
        "workflow": str(workflow),
        "import_opened": opened,
        "shortcut_name": SHORTCUT_NAME,
        "suggested_key": "Control-Command-P",
    }


def doctor() -> dict[str, Any]:
    launcher = launcher_path()
    workflow = artifact_path()
    shortcut_found = False
    tool = shutil.which("shortcuts")
    if tool:
        result = subprocess.run(
            [tool, "list"], capture_output=True, text=True, timeout=8, check=False,
        )
        shortcut_found = SHORTCUT_NAME in result.stdout.splitlines()
    checks = {
        "launcher": launcher.is_file() and os.access(launcher, os.X_OK),
        "workflow": (workflow / "Contents" / "document.wflow").is_file(),
        "shortcut": shortcut_found,
    }
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "launcher": str(launcher),
        "workflow": str(workflow),
        "shortcut_name": SHORTCUT_NAME,
    }


def uninstall() -> dict[str, Any]:
    launcher = launcher_path()
    workflow = artifact_path()
    removed = []
    if launcher.exists():
        launcher.unlink()
        removed.append(str(launcher))
    if workflow.exists():
        shutil.rmtree(workflow)
        removed.append(str(workflow))
    return {
        "removed": removed,
        "shortcut_kept": SHORTCUT_NAME,
        "next": f"remove {SHORTCUT_NAME!r} in Shortcuts if it was imported",
    }

#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def replace_unique(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    paths = {
        "gdb": ROOT / "Source/Core/Core/PowerPC/GDBStub.cpp",
        "gdb_h": ROOT / "Source/Core/Core/PowerPC/GDBStub.h",
        "mainwindow": ROOT / "Source/Core/DolphinQt/MainWindow.cpp",
    }
    src = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}

    if "ACCF_GDB_MANUAL_RESUME" in src["gdb"]:
        print("ACCF GDB manual-resume patch already applied")
        return 0
    if "ACCF_GDB_LATE_ATTACH" not in src["gdb"]:
        raise RuntimeError("Apply Tools/accf_gdb/apply_patch.py before this patch")

    # Build all outputs in memory first so a source mismatch cannot leave a
    # half-applied tree behind.
    gdb = src["gdb"]
    gdb = replace_unique(
        gdb,
        '#include <algorithm>\n\n#include <fmt/format.h>',
        '#include <algorithm>\n#include <atomic>\n\n#include <fmt/format.h>',
        "GDBStub.cpp atomic include",
    )
    gdb = replace_unique(
        gdb,
        'static bool s_has_control = false;',
        '// ACCF_GDB_MANUAL_RESUME: GUI and CPU threads both access debugger ownership.\n'
        'static std::atomic_bool s_has_control = false;',
        "GDBStub.cpp atomic control state",
    )

    gdb = replace_unique(
        gdb,
        '''  else if (!strncmp((const char*)(s_cmd_bfr), "qHostInfo", strlen("qHostInfo")))
    return WriteHostInfo();
  else if (!strncmp((const char*)(s_cmd_bfr), "qSupported", strlen("qSupported")))
    return SendReply("swbreak+;hwbreak+");''',
        '''  else if (!strncmp((const char*)(s_cmd_bfr), "qHostInfo", strlen("qHostInfo")))
    return WriteHostInfo();
  else if (!strcmp((const char*)(s_cmd_bfr), "qDolphinState"))
  {
    const auto cpu_state = Core::System::GetInstance().GetCPU().GetState();
    if (cpu_state == CPU::State::PowerDown)
      return SendReply("DolphinState:powerdown;control:0");
    if (cpu_state == CPU::State::Running)
      return SendReply("DolphinState:running;control:0");
    return SendReply(s_has_control.load() ? "DolphinState:stopped;control:1" :
                                           "DolphinState:stopped;control:0");
  }
  else if (!strncmp((const char*)(s_cmd_bfr), "qSupported", strlen("qSupported")))
    return SendReply("swbreak+;hwbreak+;qDolphinState+");''',
        "GDBStub.cpp target-state query",
    )

    gdb = replace_unique(
        gdb,
        '''  while (s_sock != -1)
  {
    if (cpu.GetState() == CPU::State::PowerDown)''',
        '''  while (s_sock != -1)
  {
    // A user can press Dolphin's Play button while GDB owns a stopped CPU.
    // ReleaseControl() is called from the GUI thread; leave the blocking GDB
    // command loop immediately so the CPU run loop can observe the GUI resume.
    if (loop_until_continue && !s_has_control.load())
      return;

    if (cpu.GetState() == CPU::State::PowerDown)''',
        "GDBStub.cpp manual-resume command-loop escape",
    )

    gdb = replace_unique(
        gdb,
        '''bool HasControl()
{
  return s_has_control;
}

void TakeControl()
{
  s_has_control = true;
}''',
        '''bool HasControl()
{
  return s_has_control.load();
}

void TakeControl()
{
  s_has_control.store(true);
}

void ReleaseControl()
{
  s_has_control.store(false);
}''',
        "GDBStub.cpp ReleaseControl API",
    )

    gdb_h = replace_unique(
        src["gdb_h"],
        '''bool HasControl();
void TakeControl();
bool JustConnected();''',
        '''bool HasControl();
void TakeControl();
void ReleaseControl();
bool JustConnected();''',
        "GDBStub.h ReleaseControl declaration",
    )

    mainwindow = src["mainwindow"]
    mainwindow = replace_unique(
        mainwindow,
        '#include "Core/Core.h"\n#include "Core/FreeLookManager.h"',
        '#include "Core/Core.h"\n#include "Core/FreeLookManager.h"\n#include "Core/PowerPC/GDBStub.h"',
        "MainWindow.cpp GDBStub include",
    )
    mainwindow = replace_unique(
        mainwindow,
        '''  if (Core::GetState(m_system) == Core::State::Paused)
  {
    Core::SetState(m_system, Core::State::Running);
  }''',
        '''  if (Core::GetState(m_system) == Core::State::Paused)
  {
    // If the pause came from GDB, the CPU thread is inside ProcessCommands(true).
    // Release debugger ownership before changing the CPU state so the normal
    // Play button and pause hotkey can resume execution without a GDB `c` packet.
    if (GDBStub::HasControl())
      GDBStub::ReleaseControl();
    Core::SetState(m_system, Core::State::Running);
  }''',
        "MainWindow.cpp manual GDB resume",
    )

    required_gdb = [
        "ACCF_GDB_MANUAL_RESUME",
        "std::atomic_bool s_has_control",
        "qDolphinState",
        "DolphinState:running;control:0",
        "loop_until_continue && !s_has_control.load()",
        "void ReleaseControl()",
        "qDolphinState+",
    ]
    for token in required_gdb:
        if token not in gdb:
            raise RuntimeError(f"GDBStub.cpp validation failed: missing token {token!r}")
    if "GDBStub::ReleaseControl();" not in mainwindow:
        raise RuntimeError("MainWindow.cpp validation failed: manual ReleaseControl call missing")
    if "void ReleaseControl();" not in gdb_h:
        raise RuntimeError("GDBStub.h validation failed: ReleaseControl declaration missing")

    outputs = {
        paths["gdb"]: gdb,
        paths["gdb_h"]: gdb_h,
        paths["mainwindow"]: mainwindow,
    }
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8", newline="\n")

    print("Applied ACCF GDB manual-resume patch atomically")
    print("Dolphin Play/TogglePause can now release GDB control and resume the CPU")
    print("Custom RSP query: qDolphinState")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

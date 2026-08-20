#!/usr/bin/env python3
from pathlib import Path
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[2]


def block(text: str) -> str:
    return textwrap.dedent(text).strip("\n")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def main() -> int:
    gdb = ROOT / "Source/Core/Core/PowerPC/GDBStub.cpp"
    core = ROOT / "Source/Core/Core/Core.cpp"
    settings_cpp = ROOT / "Source/Core/Core/Config/MainSettings.cpp"
    settings_h = ROOT / "Source/Core/Core/Config/MainSettings.h"

    marker = "ACCF_GDB_LATE_ATTACH"
    if marker in gdb.read_text(encoding="utf-8"):
        print("ACCF GDB patch already applied")
        return 0

    # Add a normal Dolphin configuration value. This makes the interval available through
    # Dolphin.ini and the existing -C command-line override mechanism.
    replace_once(
        settings_cpp,
        'const Info<int> MAIN_GDB_PORT{{System::Main, "General", "GDBPort"}, -1};',
        block(
            '''
            const Info<int> MAIN_GDB_PORT{{System::Main, "General", "GDBPort"}, -1};
            const Info<int> MAIN_GDB_UPDATE_CYCLES{
                {System::Main, "General", "GDBUpdateCycles"}, 5000000};
            '''
        ),
    )
    replace_once(
        settings_h,
        'extern const Info<int> MAIN_GDB_PORT;',
        'extern const Info<int> MAIN_GDB_PORT;\nextern const Info<int> MAIN_GDB_UPDATE_CYCLES;',
    )

    replace_once(gdb, '#include <fmt/format.h>', '#include <algorithm>\n\n#include <fmt/format.h>')
    replace_once(
        gdb,
        '#include "Core/Core.h"\n#include "Core/HW/CPU.h"',
        '#include "Core/Config/MainSettings.h"\n#include "Core/Core.h"\n#include "Core/HW/CPU.h"',
    )
    replace_once(
        gdb,
        'const s64 GDB_UPDATE_CYCLES = 100000;',
        block(
            '''
            // ACCF_GDB_LATE_ATTACH: configurable, low-overhead polling interval.
            constexpr s64 MIN_GDB_UPDATE_CYCLES = 1000;

            static s64 GetGDBUpdateCycles()
            {
              return std::max<s64>(MIN_GDB_UPDATE_CYCLES,
                                   Config::Get(Config::MAIN_GDB_UPDATE_CYCLES));
            }
            '''
        ),
    )

    socket_helpers = block(
        '''
        static CoreTiming::EventType* s_update_event;

        static bool IsSocketReady(int sock, int timeout_us)
        {
          if (sock < 0)
            return false;

          timeval timeout = {};
          timeout.tv_usec = timeout_us;

          fd_set fds;
          FD_ZERO(&fds);
          FD_SET(sock, &fds);

          const int result = select(sock + 1, &fds, nullptr, nullptr, &timeout);
          if (result < 0)
          {
            ERROR_LOG_FMT(GDB_STUB, "gdb: select failed");
            return false;
          }
          return result > 0 && FD_ISSET(sock, &fds);
        }

        static void CloseSocket(int& sock)
        {
          if (sock == -1)
            return;

          shutdown(sock, SHUT_RDWR);
        #ifdef _WIN32
          closesocket(sock);
        #else
          close(sock);
        #endif
          sock = -1;
        }

        static void DisconnectClient()
        {
          CloseSocket(s_sock);
          s_has_control = false;
          s_just_connected = false;
        }

        static void TryAcceptClient()
        {
          if (s_sock != -1 || s_tmpsock == -1 || !IsSocketReady(s_tmpsock, 0))
            return;

          s_sock = accept(s_tmpsock, nullptr, nullptr);
          if (s_sock < 0)
          {
            ERROR_LOG_FMT(GDB_STUB, "Failed to accept gdb client");
            s_sock = -1;
            return;
          }

          INFO_LOG_FMT(GDB_STUB, "GDB client connected (late attach).");
          s_just_connected = true;
          // Attaching must not pause the game by itself. The client can send Ctrl+C when it
          // actually wants control.
          s_has_control = false;
        }

        static const char* CommandBufferAsString()
        '''
    )
    replace_once(
        gdb,
        'static CoreTiming::EventType* s_update_event;\n\nstatic const char* CommandBufferAsString()',
        socket_helpers,
    )

    replace_once(
        gdb,
        block(
            '''
            static void UpdateCallback(Core::System& system, u64 userdata, s64 cycles_late)
            {
              ProcessCommands(false);
              if (IsActive())
                Core::System::GetInstance().GetCoreTiming().ScheduleEvent(GDB_UPDATE_CYCLES, s_update_event);
            }
            '''
        ),
        block(
            '''
            static void UpdateCallback(Core::System& system, u64 userdata, s64 cycles_late)
            {
              if (s_sock == -1)
                TryAcceptClient();
              else
                ProcessCommands(false);

              if (IsActive())
              {
                Core::System::GetInstance().GetCoreTiming().ScheduleEvent(GetGDBUpdateCycles(),
                                                                          s_update_event);
              }
            }
            '''
        ),
    )

    replace_once(
        gdb,
        block(
            '''
              if (res != 1)
              {
                ERROR_LOG_FMT(GDB_STUB, "recv failed : {}", res);
                Deinit();
              }
            '''
        ),
        block(
            '''
              if (res != 1)
              {
                ERROR_LOG_FMT(GDB_STUB, "recv failed : {}", res);
                DisconnectClient();
              }
            '''
        ),
    )

    replace_once(
        gdb,
        block(
            '''
            static bool IsDataAvailable()
            {
              timeval t;
              fd_set _fds, *fds = &_fds;

              FD_ZERO(fds);
              FD_SET(s_sock, fds);

              t.tv_sec = 0;
              t.tv_usec = 20;

              if (select(s_sock + 1, fds, nullptr, nullptr, &t) < 0)
              {
                ERROR_LOG_FMT(GDB_STUB, "select failed");
                return false;
              }

              if (FD_ISSET(s_sock, fds))
                return true;
              return false;
            }
            '''
        ),
        block(
            '''
            static bool IsDataAvailable()
            {
              return IsSocketReady(s_sock, 20);
            }
            '''
        ),
    )

    replace_once(
        gdb,
        '  if (!IsActive())\n    return;\n\n  memset(s_cmd_bfr, 0, sizeof s_cmd_bfr);',
        '  if (s_sock == -1)\n    return;\n\n  memset(s_cmd_bfr, 0, sizeof s_cmd_bfr);',
    )
    replace_once(
        gdb,
        block(
            '''
                if (n < 0)
                {
                  ERROR_LOG_FMT(GDB_STUB, "gdb: send failed");
                  return Deinit();
                }
            '''
        ),
        block(
            '''
                if (n < 0)
                {
                  ERROR_LOG_FMT(GDB_STUB, "gdb: send failed");
                  DisconnectClient();
                  return;
                }
            '''
        ),
    )

    replace_once(
        gdb,
        '  while (IsActive())\n  {\n    if (cpu.GetState() == CPU::State::PowerDown)',
        '  while (s_sock != -1)\n  {\n    if (cpu.GetState() == CPU::State::PowerDown)',
    )
    replace_once(
        gdb,
        block(
            '''
                case 'k':
                  Deinit();
                  INFO_LOG_FMT(GDB_STUB, "killed by gdb");
                  return;
            '''
        ),
        block(
            '''
                case 'k':
                  DisconnectClient();
                  INFO_LOG_FMT(GDB_STUB, "gdb client disconnected");
                  return;
            '''
        ),
    )

    # Keep a listener alive instead of blocking in accept() during game boot.
    replace_once(
        gdb,
        block(
            '''
            static void InitGeneric(int domain, const sockaddr* server_addr, socklen_t server_addrlen,
                                    sockaddr* client_addr, socklen_t* client_addrlen);
            '''
        ),
        'static void InitGeneric(int domain, const sockaddr* server_addr, socklen_t server_addrlen);',
    )
    replace_once(
        gdb,
        '  InitGeneric(PF_LOCAL, (const sockaddr*)&addr, sizeof(addr), nullptr, nullptr);',
        '  InitGeneric(PF_LOCAL, (const sockaddr*)&addr, sizeof(addr));',
    )

    replace_once(
        gdb,
        block(
            '''
            void Init(u32 port)
            {
              sockaddr_in saddr_server = {};
              sockaddr_in saddr_client;

              saddr_server.sin_family = AF_INET;
              saddr_server.sin_port = htons(port);
              saddr_server.sin_addr.s_addr = INADDR_ANY;

              socklen_t client_addrlen = sizeof(saddr_client);

              InitGeneric(PF_INET, (const sockaddr*)&saddr_server, sizeof(saddr_server),
                          (sockaddr*)&saddr_client, &client_addrlen);

              saddr_client.sin_addr.s_addr = ntohl(saddr_client.sin_addr.s_addr);
            }
            '''
        ),
        block(
            '''
            void Init(u32 port)
            {
              sockaddr_in saddr_server = {};

              saddr_server.sin_family = AF_INET;
              saddr_server.sin_port = htons(port);
              // The GDB stub is unauthenticated. Keep this debugging listener local-only.
              saddr_server.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

              InitGeneric(PF_INET, (const sockaddr*)&saddr_server, sizeof(saddr_server));
            }
            '''
        ),
    )
    replace_once(
        gdb,
        block(
            '''
            static void InitGeneric(int domain, const sockaddr* server_addr, socklen_t server_addrlen,
                                    sockaddr* client_addr, socklen_t* client_addrlen)
            '''
        ),
        'static void InitGeneric(int domain, const sockaddr* server_addr, socklen_t server_addrlen)',
    )

    replace_once(
        gdb,
        block(
            '''
              INFO_LOG_FMT(GDB_STUB, "Waiting for gdb to connect...");

              s_sock = accept(s_tmpsock, client_addr, client_addrlen);
              if (s_sock < 0)
                ERROR_LOG_FMT(GDB_STUB, "Failed to accept gdb client");
              INFO_LOG_FMT(GDB_STUB, "Client connected.");
              s_just_connected = true;

            #ifdef _WIN32
              closesocket(s_tmpsock);
            #else
              close(s_tmpsock);
            #endif
              s_tmpsock = -1;

              auto& system = Core::System::GetInstance();
              auto& core_timing = system.GetCoreTiming();
              s_update_event = core_timing.RegisterEvent("GDBStubUpdate", UpdateCallback);
              core_timing.ScheduleEvent(GDB_UPDATE_CYCLES, s_update_event);
              s_has_control = true;
            '''
        ),
        block(
            '''
              INFO_LOG_FMT(GDB_STUB, "GDB stub listening for late attach...");

              auto& system = Core::System::GetInstance();
              auto& core_timing = system.GetCoreTiming();
              s_update_event = core_timing.RegisterEvent("GDBStubUpdate", UpdateCallback);
              core_timing.ScheduleEvent(GetGDBUpdateCycles(), s_update_event);
              s_has_control = false;
            '''
        ),
    )

    replace_once(
        gdb,
        block(
            '''
            void Deinit()
            {
              if (s_tmpsock != -1)
              {
                shutdown(s_tmpsock, SHUT_RDWR);
                s_tmpsock = -1;
              }
              if (s_sock != -1)
              {
                shutdown(s_sock, SHUT_RDWR);
                s_sock = -1;
              }

              s_socket_context.reset();
              s_has_control = false;
            }
            '''
        ),
        block(
            '''
            void Deinit()
            {
              DisconnectClient();
              CloseSocket(s_tmpsock);
              s_socket_context.reset();
              s_has_control = false;
            }
            '''
        ),
    )

    # Enabling GDB must no longer force the initial CPU state to Paused.
    replace_once(
        core,
        '      GDBStub::InitLocal(gdb_socket.data());\n      CPUSetInitialExecutionState(system, true);',
        '      GDBStub::InitLocal(gdb_socket.data());\n      CPUSetInitialExecutionState(system);',
    )
    replace_once(
        core,
        '        GDBStub::Init(gdb_port);\n        CPUSetInitialExecutionState(system, true);',
        '        GDBStub::Init(gdb_port);\n        CPUSetInitialExecutionState(system);',
    )

    print("Applied ACCF GDB late-attach patch")
    print("Default GDBUpdateCycles: 5000000")
    print("Dolphin.ini: [General] GDBUpdateCycles = <cycles>")
    print("CLI: -C Dolphin.General.GDBUpdateCycles=<cycles>")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

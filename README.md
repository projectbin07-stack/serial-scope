# serial-scope

[![CI](https://github.com/projectbin07-stack/serial-scope/actions/workflows/ci.yml/badge.svg)](https://github.com/projectbin07-stack/serial-scope/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A serial monitor, logger and live plotter that understands structured telemetry, for people
who live in the Arduino serial monitor.

![serial-scope replaying a synthetic motor speed-loop log, paused, with the cursor readout and trigger markers](docs/screenshot-plotter-light.png)

*The screenshots show replayed synthetic demo data (`serial-scope demo`), not a live board.*

> **Status:** v0.2.0. Everything below is implemented and tested in CI on Linux and Windows,
> but **serial-scope has not yet been connected to a real board**, and the command sending
> added in 0.2.0 **has never driven a real motor**. See
> [Hardware validation status](#hardware-validation-status).

## Why it exists

When firmware prints telemetry, the usual loop is: open the Arduino serial monitor, watch
numbers scroll, switch to the serial plotter, lose the data when the window closes, and copy
lines into a spreadsheet when something odd happens. Binary or checksummed formats are not
readable at all without writing a one-off script.

serial-scope keeps that workflow (a terminal with a send box, a live plot) and adds what is
missing for debugging: it decodes several line and binary formats, counts every checksum and
framing error instead of hiding it, records every byte with timestamps so a session can be
replayed exactly, and does all of that from a scriptable command line as well as a GUI.

### What exists already, and what this adds

| Tool | What it does well | What it does not do (as far as I know, at the time of writing) |
| --- | --- | --- |
| Arduino IDE 2 Serial Monitor / Plotter | Zero setup; plots `label:value` lines | No log file, no replay, no checksummed or binary formats; the port is held by one window |
| PuTTY | Robust terminal, session logging | No plotting, no decoding |
| CoolTerm | Terminal with hex view, capture to file | No plotting, no structured decoding |
| SerialPlot | Fast Qt plotting of ASCII, fixed binary layouts and simple custom frames | No COBS / CRC-16 framing, no JSON lines, no raw byte log for exact replay, no headless mode |
| pyserial `miniterm` | Minimal terminal in Python, hex filters | No plotting, logging or decoding |

serial-scope adds:

- **Decoding of several line and framed formats**, auto-detected or forced: plain text,
  Arduino-plotter lines, CSV with a header, JSON lines, the `$BM1` CRC-8 lines of
  [ina219-battery-monitor](https://github.com/projectbin07-stack/ina219-battery-monitor), the
  COBS + CRC-16 binary frames of
  [encoder-odometry-firmware](https://github.com/projectbin07-stack/encoder-odometry-firmware)
  and the USB packets of the
  [nrf24-telemetry-link](https://github.com/projectbin07-stack/nrf24-telemetry-link) radio
  gateway. CRC, framing and format errors are counted and shown; sequence gaps and device
  restarts too.
- **Radio link quality** from the nrf24 gateway's own counters (CRC errors, lost messages,
  retries, failed commands) as plot channels, totals, and a notice whenever one goes up.
- **Commands for encoder-odometry-firmware**: velocity, stop and parameter get / set, from
  the command line (`serial-scope send`) or a GUI panel, byte-for-byte equal to the
  firmware's published test vectors. These move motors; read
  [the safety notes](#sending-commands-to-encoder-odometry-firmware) first.
- **Auto-discovered numeric channels** with ring buffers, min / max / mean and sample rate,
  plotted on host time or the device's own clock.
- **Reliable logging and replay**: every received and sent byte, with microsecond host
  timestamps, in a compact `.sslog` file that is flushed per record, rotates by size and can be
  replayed byte for byte at original or accelerated speed. Decoded CSV export.
- **Triggers**: thresholds with hysteresis, rising / falling edges and regular expressions on
  text, with mark, beep, stop-logging-after-N-seconds and snapshot actions.
- **A scriptable headless mode** with exit codes (`--max-errors`) for use in scripts and CI.
- **Auto-reconnect** when a board is unplugged, reset into its bootloader or re-enumerated.

What it does not try to be: a VT100 terminal emulator, a file-transfer tool, or a
high-performance plotter. The plot is drawn on a Tk Canvas and decimated to the window width;
it is meant for tens of channels at tens to hundreds of samples per second, and its
throughput limits have not been measured.

## Install

serial-scope needs Python 3.10 or newer. The only runtime dependency is pyserial; the GUI
uses Tk, which ships with the python.org installers for Windows and macOS (on Debian/Ubuntu:
`sudo apt install python3-tk`). It is not published on PyPI; install from GitHub:

```console
pip install git+https://github.com/projectbin07-stack/serial-scope.git
```

or from a clone with `pip install -e .`.

## Quick start on Windows

1. **Find the COM port.** Plug the board in and run `serial-scope list`. Each port is shown
   with its USB VID:PID and, for common chips, what it probably is:

   ```text
   COM5  10C4:EA60  Silicon Labs CP210x USB to UART Bridge (COM5)  [Silicon Labs CP210x (ESP32 DevKit, NodeMCU)]
   ```

   (That line shows the format; the machine this README was written on has no serial ports,
   so its real output was `no serial ports found`.) Without serial-scope: Device Manager >
   Ports (COM & LPT), then unplug and replug the board and see which entry disappears.
   CH340 boards (most Nano clones) need the WCH driver on older Windows versions; boards with
   native USB (ESP32-S3/C3, RP2040, Arduino Leonardo) appear as "USB Serial Device".
2. **Close other programs using the port.** Windows allows one program per COM port. If the
   Arduino IDE serial monitor, PlatformIO's monitor or an upload is running, opening fails with
   "Access is denied" and serial-scope keeps retrying until the port is free.
3. **Watch it.** `serial-scope gui` and pick the port, or from the terminal:

   ```console
   serial-scope monitor COM5 -b 115200 --log run.sslog
   ```

4. **Look at it again later.** `serial-scope replay run.sslog --speed 4`, `serial-scope stats
   run.sslog`, or open it in the GUI with *Replay...*.

No board at hand? `serial-scope demo demo.sslog` writes a synthetic session (an ESP32-style
motor speed loop at 50 Hz); `serial-scope gui --replay demo.sslog` plays it. `--kind` picks
another format: `ina219`, `odometry`, `nrf24` (a radio gateway with link statistics),
`jsonl` or `csv`.

## Supported formats

Pick one with `--decode NAME` or leave the default `auto`. Auto-detection feeds the first
records to every decoder in parallel and commits to the most specific one that decodes at least
five records with few errors (or after 4 KiB / 3 s, whichever comes first). Everything received
during detection is then released by the winning decoder, so nothing is lost.

| Name | Example | Channels | Device time |
| --- | --- | --- | --- |
| `plotter` | `temp:21.5,hum:40` / `1.5 2.25 -3` | labels, or `ch1`, `ch2`, ... by position | - |
| `csv` | header `millis,adc0,adc1` then `50,2048,1000` | one per column | a `t_ms` / `millis` / `t_us` / `time` column |
| `jsonl` | `{"t_ms":1500,"temp":21.5,"imu":{"ax":0.1}}` | numbers and booleans, flattened (`imu.ax`) | a top-level `t_ms` / `millis` / ... key |
| `ina219` | `$BM1,42,21000,3876,100000,386000,583,2261,512,0*D1` | `bus_v`, `current_ma`, `power_mw`, `charge_mah`, `energy_mwh`, `soc_pct`, `flags` | `t_ms` |
| `odometry` | binary: `COBS(ver type seq len body crc16) 00` | `fl.velocity`, `odom.x`, `diag.max_loop_us`, ... | `t_us` (32-bit wrap handled) |
| `nrf24` | binary: `COBS(ver kind seq len body crc16) 00` from the radio gateway | `odom.x`, `imu.gz`, `battery.bus_v`, `kv.<key>`, `link.crc_errors`, ... | node `t_ms` (odometry, IMU), gateway `uptime_ms` (link stats) |
| `text` | anything | none | - |

Details:

- **Lines** may end in LF, CR LF or a lone CR, and may be split across any number of reads.
  Lines longer than 4096 bytes are reported as `overlong` and skipped. Invalid UTF-8 is shown
  with replacement characters rather than dropped. Lines that are not data (boot messages,
  `Serial.println("WiFi connected")`) are passed through as text in every line format.
- **plotter** follows the Arduino IDE 2 plotter conventions: values separated by commas,
  spaces or tabs, optionally `label:value`. A line with any token that is neither is text.
  `nan` / `inf` are recognised and left out of the plot.
- **csv** errors: `columns` (wrong number of fields) and `value` (a non-numeric field). A
  repeated header (after a board reset) is accepted and may change the columns.
- **ina219** re-implements the format documented in ina219-battery-monitor: CRC-8 (poly
  0x07) over everything between `$` and `*`. Errors: `crc`, `format`. Sequence numbers give
  `dropped` (frames missing) and `restarts` (counter went backwards). `#...` lines are text.
- **odometry** re-implements protocol version 1 of encoder-odometry-firmware
  ([docs/protocol.md](https://github.com/projectbin07-stack/encoder-odometry-firmware/blob/main/docs/protocol.md)):
  COBS framing with a `00` delimiter, CRC-16/CCITT-FALSE, all eight message types. Errors:
  `overlong` (more than 72 bytes between delimiters, e.g. the ESP32 ROM boot text), `cobs`,
  `short`, `crc`, `version`, `type`, `length`. Sequence gaps are counted as `lost`. A
  receiver that starts mid-stream, or after boot text without a delimiter, loses the first
  frame and resynchronises at the next `00`, as the protocol specifies.

- **nrf24** re-implements the gateway USB packets (section 3) and the application messages
  (section 2) of nrf24-telemetry-link's
  [docs/protocol.md](https://github.com/projectbin07-stack/nrf24-telemetry-link/blob/main/docs/protocol.md).
  The packet layout is the odometry one with a different kind table and bodies of up to 255
  bytes, so frames may be 263 bytes long. Errors: `overlong`, `cobs`, `short`, `crc`,
  `version`, `length`, `kind` (unknown packet kind) and `message` (an invalid battery /
  odometry / IMU / key-value / command message inside a valid packet). Gaps in the
  gateway-to-host sequence number are counted as `lost`; a packet rejected for its CRC also
  leaves a gap, so it is counted both as a `crc` error and as `lost`.

  | Message | Channels | Units and notes |
  | --- | --- | --- |
  | battery | `battery.bus_v`, `.current_ma`, `.power_mw`, `.charge_mah`, `.soc_pct`, `.flags` | V, mA, mW, mAh, %. Current, power and charge are left out when flag bit 7 (no current sensor) is set; `soc_pct` when the node sends 65535 (unknown) |
  | odometry | `odom.x`, `odom.y`, `odom.theta`, `odom.v`, `odom.w` | m, m, rad, m/s, rad/s |
  | imu | `imu.ax`, `.ay`, `.az`, `imu.gx`, `.gy`, `.gz`, `imu.temp_c` | g, deg/s, deg C |
  | key/value | `kv.<key>` for int, float and bool (0/1) values | the whole message is also a `KV key=value ...` text line, strings included |
  | link stats | `link.frames_received`, `link.crc_errors`, `link.unreliable_lost`, `link.reliable_retries`, ... (all 16 counters and state fields) | the gateway's counts since it booted |
  | command result, gateway log | text lines `COMMAND_RESULT link_seq=12 delivered`, `[gateway] ...` | |

  **Radio link quality.** The gateway reports its radio counters once a second. serial-scope
  plots them as `link.*` channels, shows the latest totals of `frames_received`,
  `crc_errors`, `malformed`, `duplicates`, `unreliable_lost`, `reliable_retries`,
  `reliable_failed` and `resyncs` as "radio link" in the summary, in `stats` and in the GUI's
  Stats tab, and reports a **notice** each time `crc_errors`, `malformed`,
  `unreliable_lost`, `reliable_retries`, `reliable_failed` or `resyncs` goes up between two
  statistics packets (`~ link: radio: crc_errors +2 (total 4)`). A command the gateway could
  not deliver is a notice too, and counters going backwards are reported as a gateway
  restart. Notices are not decode errors (the bytes on the USB line were fine), so
  `--max-errors` ignores them; to act on them use a trigger such as `re:unreliable_lost` or
  `link.crc_errors > 10`.

All three firmware formats are tested against the reference data published in those
repositories (copied into [`tests/data`](tests/data/SOURCES.md) with their origin and commit).

## Command line

```text
$ serial-scope --help
usage: serial-scope [-h] [--version]
                    {list,monitor,replay,stats,send,decoders,demo,gui} ...

Serial monitor, logger and live plotter for structured telemetry.

positional arguments:
  {list,monitor,replay,stats,send,decoders,demo,gui}
    list                list serial ports with USB VID:PID
    monitor             read a serial port or TCP socket
    replay              play back a .sslog file
    stats               summarise a .sslog file
    send                send a velocity or parameter command to encoder-
                        odometry-firmware
    decoders            list the available decoders
    demo                write a synthetic demo log (no hardware needed)
    gui                 open the graphical monitor

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
```

`monitor` and `replay` share these options (`serial-scope monitor --help`):

```text
  -b, --baud BAUD       baud rate (default 115200)
  --no-reconnect        exit when the link drops
  --send TEXT           send TEXT once connected (\n etc. allowed)
  --decode {auto,ina219,odometry,nrf24,jsonl,csv,plotter,text}
                        decoder (default: auto-detect)
  --log FILE.sslog      record every byte to a raw log
  --rotate-mb MB        start a new log part every MB
  --csv FILE.csv        export decoded samples as CSV
  --show {decoded,text,samples,raw,hex,none}
                        what to print: decoded events (default), text lines
                        only, samples only, raw bytes, a hex dump, or nothing
  --trigger SPEC        e.g. 'current_ma > 500 -> mark,snapshot', 'rise:v@3.3
                        -> beep', '/panic/ -> mark,stop:2' (repeatable)
  --hysteresis HYSTERESIS
                        threshold re-arm band
  --holdoff HOLDOFF     min seconds between firings
  --snapshot-dir SNAPSHOT_DIR
                        where snapshot CSVs go (default: .)
  --buffer BUFFER       points kept per channel
  --time-base {host,device}
                        x axis for snapshots: host receive time or device time
  --duration DURATION   stop after this many seconds
  --abs-time            print wall-clock times
  --max-errors N        exit with status 3 if there are more than N decode
                        errors
  -q, --quiet           no summary on stderr
```

(`--send` and `--no-reconnect` are `monitor` only; `replay` adds `--speed`, where 1 is the
original pace, 4 is four times faster and 0, the default, is as fast as possible.)

Sources for `monitor`: `COM5`, `/dev/ttyUSB0`, `tcp://192.168.4.1:3333` (an ESP32 serving
telemetry over Wi-Fi) or any [pyserial URL](https://pyserial.readthedocs.io/en/latest/url_handlers.html)
such as `rfc2217://host:port` or `loop://`.

Exit status: 0 on success, 2 for bad arguments or a source that could not be opened
(`--no-reconnect`), 3 when `--max-errors` is exceeded. `send` has its own codes, listed
[below](#sending-commands-to-encoder-odometry-firmware).

### Real output from the demo logs

Every block below is the unedited output of the command shown, run on the synthetic demo logs
(times are offsets from the first byte; the summary goes to stderr).

```text
$ serial-scope demo demo.sslog
wrote demo.sslog: synthetic 'plotter' session, 20 s, 1761 chunks, 43878 bytes

$ serial-scope stats demo.sslog
file        demo.sslog  (1 part(s), 43878 bytes)
source      demo:plotter  (synthetic demo data)
time        2025-11-19 11:00:00.000Z .. 2025-11-19 11:00:20.400Z
duration    20.401 s
records     rx 1761 (35844 bytes), tx 0, marks 0, info 0
decoder     auto (plotter)
decoded     1001 samples, 10 text lines
errors      none

channel      count         min         max        mean  rate Hz
setpoint      1001           0         150     82.5375     50.0
speed         1001      -45.17      176.77     82.5372     50.0
duty          1001       -60.7         100     45.9918     50.0
```

Triggers, text lines only, and a decoded CSV export (`re:` is the regex form that Git Bash
does not mistake for a path; `/RSSI/` works in cmd and PowerShell):

```text
$ serial-scope replay demo.sslog --show text --trigger "speed > 160 -> mark,snapshot" --trigger "re:RSSI -> mark" --csv demo.csv
[connected to replay:demo.sslog]
+    0.000s  ets Jun  8 2016 00:22:57
+    0.000s  
+    0.000s  rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
+    0.000s  configsip: 0, SPIWP:0xee
+    0.001s  mode:DIO, clock div:2
+    0.001s  entry 0x400805e4
+    0.250s  [app] speed loop demo, 50 Hz
+    5.421s  [wifi] RSSI -61 dBm
+    5.421s  * trigger re:RSSI: line matched /RSSI/: [wifi] RSSI -61 dBm [mark]
+   10.421s  [wifi] RSSI -60 dBm
+   10.421s  * trigger re:RSSI: line matched /RSSI/: [wifi] RSSI -60 dBm [mark]
+   11.720s  * trigger speed > 160: speed = 162.07 > 160 [mark, snapshot]
+   15.421s  [wifi] RSSI -63 dBm
+   15.421s  * trigger re:RSSI: line matched /RSSI/: [wifi] RSSI -63 dBm [mark]
[replay:demo.sslog: end of data]
decoder auto (plotter); 35844 bytes in 20.401 s; 1001 samples, 10 text lines; errors: none
4 markers
snapshots: snapshot-20251119-163011-720.csv

$ head -3 demo.csv
host_time,device_time,source,setpoint,speed,duty
1763550000.400000,,plotter,0,1.03,0
1763550000.420000,,plotter,0,1.16,0
```

The ina219 demo contains one frame with a corrupted digit and one frame that never arrived.
Both show up, the first as a `crc` error, the pair as `dropped 2` in the sequence counter:

```text
$ serial-scope demo ina219.sslog --kind ina219
wrote ina219.sslog: synthetic 'ina219' session, 20 s, 450 chunks, 12364 bytes

$ serial-scope replay ina219.sslog --show text
[connected to replay:ina219.sslog]
+    0.000s  #ina219-battery-monitor 0.1.0
+    0.050s  #OK RATE 100
+    5.800s  $BM1,57,5799,3901,149664,583689,241,946,779,0*CD
+    5.800s  ! crc: checksum mismatch: got CD, computed E2
[replay:ina219.sslog: end of data]
decoder auto (ina219); 10319 bytes in 19.900 s; 197 samples, 3 text lines; errors: crc 1
sequence: dropped 2
```

The odometry demo starts with ESP32 ROM boot text on the binary port and contains one frame
with a flipped bit:

```text
$ serial-scope replay odometry.sslog --show text --speed 4 --duration 1
[connected to replay:odometry.sslog]
+    0.000s  ! overlong: more than 72 bytes between delimiters (starts b'ets Jun  8 2016 00:22:57')
+    2.600s  ! crc: CRC mismatch: frame has 4FA8, computed DC35
decoder auto (odometry); 8441 bytes in 4.000 s; 157 samples, 0 text lines; errors: crc 1, overlong 1
sequence: lost 1

$ serial-scope stats odometry.sslog
file        odometry.sslog  (1 part(s), 51165 bytes)
source      demo:odometry  (synthetic demo data)
time        2025-11-19 11:00:00.000Z .. 2025-11-19 11:00:19.951Z
duration    19.952 s
records     rx 1905 (43247 bytes), tx 0, marks 0, info 0
decoder     auto (odometry)
decoded     829 samples, 0 text lines
errors      crc 1, overlong 1
sequence    lost 1

channel                count         min         max        mean  rate Hz
odom.x                   396   -0.757384    0.742619   0.0668656     20.0
odom.y                   396  -0.000989697     1.49901      0.6278     20.0
odom.theta               396    -3.13619       3.127    0.108552     20.0
...
```

The nrf24 demo is a gateway relaying a robot node's telemetry: odometry and IMU at 10 Hz,
battery at 1 Hz, a key/value status every 5 s and the gateway's link statistics every
second. It contains radio losses and CRC errors (reported by the gateway, shown as
notices), one reliable command delivered after a retry and one that failed, and on the USB
side one packet with a flipped bit and one missing packet (`lost 2`: the corrupted packet's
sequence number never arrived intact either):

```text
$ serial-scope demo nrf24.sslog --kind nrf24
wrote nrf24.sslog: synthetic 'nrf24' session, 20 s, 752 chunks, 17299 bytes

$ serial-scope replay nrf24.sslog --show text
[connected to replay:nrf24.sslog]
+    0.000s  [gateway] gateway ready
+    2.050s  ~ link: radio: crc_errors +1 (total 1)
+    3.050s  ~ link: radio: unreliable_lost +1 (total 2)
+    4.050s  ~ link: radio: crc_errors +1 (total 2), unreliable_lost +1 (total 3)
+    5.050s  ~ link: radio: crc_errors +2 (total 4)
+    5.812s  KV period_ms=100 pa_dbm=-6 imu_ok=true fw=demo
+    6.050s  ~ link: radio: unreliable_lost +3 (total 6)
+    6.400s  ! crc: CRC mismatch: frame has E169, computed 81D3
+    8.130s  COMMAND_RESULT link_seq=12 delivered
+    9.050s  ~ link: radio: crc_errors +1 (total 5), reliable_retries +1 (total 1)
+   10.050s  ~ link: radio: unreliable_lost +1 (total 7)
+   10.812s  KV period_ms=100 pa_dbm=-6 imu_ok=true fw=demo
+   12.050s  ~ link: radio: crc_errors +1 (total 6), unreliable_lost +2 (total 9)
+   14.130s  COMMAND_RESULT link_seq=13 NOT delivered
+   14.130s  ~ link: gateway: command with link seq 13 not delivered
+   15.050s  ~ link: radio: reliable_retries +7 (total 8), reliable_failed +1 (total 1), resyncs +1 (total 1)
+   15.812s  KV period_ms=100 pa_dbm=-6 imu_ok=true fw=demo
+   17.050s  ~ link: radio: unreliable_lost +1 (total 10)
+   18.050s  ~ link: radio: unreliable_lost +1 (total 11)
[replay:nrf24.sslog: end of data]
decoder auto (nrf24); 14031 bytes in 19.904 s; 428 samples, 6 text lines; errors: crc 1
sequence: lost 2
radio link (device totals): frames_received 8609, crc_errors 6, malformed 0, duplicates 0, unreliable_lost 11, reliable_retries 8, reliable_failed 1, resyncs 1
notices: link 12

$ serial-scope stats nrf24.sslog
file        nrf24.sslog  (1 part(s), 17299 bytes)
source      demo:nrf24  (synthetic demo data)
time        2025-11-19 11:00:00.000Z .. 2025-11-19 11:00:19.904Z
duration    19.904 s
records     rx 752 (14031 bytes), tx 0, marks 0, info 0
decoder     auto (nrf24)
decoded     428 samples, 6 text lines
errors      crc 1
sequence    lost 2
radio link  frames_received 8609, crc_errors 6, malformed 0, duplicates 0, unreliable_lost 11, reliable_retries 8, reliable_failed 1, resyncs 1  (device totals)
notices     link 12

channel                     count         min         max        mean  rate Hz
odom.x                        192      -0.727       0.702  -0.00669271      9.6
odom.y                        192      -0.001       1.427    0.632167      9.6
odom.theta                    192      -3.116       3.133     0.01125      9.6
...
```

The radio counters start high (8200 frames before the log began) because the gateway
counts from its own boot, not from when serial-scope connected.

pyserial's `loop://` device echoes what is written to it, which exercises the real serial code
path without hardware:

```text
$ serial-scope monitor loop:// --send 'temp:21.5,hum:40\r\n' --duration 0.5 --log loop.sslog
[connected to loop://@115200]
decoder auto (plotter); 18 bytes in 0.000 s; 1 samples, 0 text lines; errors: none
+    0.000s  temp=21.5 hum=40
```

### Scripting examples

```console
# Log a long run, one file per 50 MB, CSV alongside, and mark every brown-out message
serial-scope monitor COM5 -b 921600 --log soak.sslog --rotate-mb 50 --csv soak.csv \
    --show none --trigger "re:Brownout -> mark,beep"

# Fail a hardware-in-the-loop script if the link produced any CRC or framing error
serial-scope replay soak.sslog --show none --max-errors 0

# Stop recording two seconds after a panic, keeping the lead-up
serial-scope monitor COM7 --log crash.sslog --trigger "re:Guru Meditation -> mark,stop:2"

# Machine-readable summary
serial-scope stats soak.sslog --json
```

## Sending commands to encoder-odometry-firmware

> **Safety: velocity commands move motors.** The first time, put the robot on a stand with
> its wheels off the ground and keep the motor power switch within reach.
> encoder-odometry-firmware stops the motors by itself when no valid velocity command has
> arrived for `watchdog_ms` (500 ms by default), so a single `vel` drives for about half a
> second, and if serial-scope, the cable or the PC stops mid-command it is the **firmware's
> watchdog** that stops the robot. Do not raise `watchdog_ms` further than you would trust
> that. serial-scope's command path has only been checked against the firmware's published
> byte vectors and through pyserial's `loop://` device; **it has never driven a real
> motor**.

```console
serial-scope send COM5 --odometry vel 0.2 0 0.1           # vx m/s, vy m/s, wz rad/s, once
serial-scope send COM5 --odometry vel 0.2 0 0.1 --hold 3  # repeat at 10 Hz for 3 s, then stop
serial-scope send COM5 --odometry stop                    # zero velocity now
serial-scope send COM5 --odometry get-param watchdog_ms
serial-scope send COM5 --odometry set-param kp 0.03
```

- Put the port before `--odometry`, which takes all the words after it.
- Parameters are given by the names (or ids) in the firmware's
  [parameter table](https://github.com/projectbin07-stack/encoder-odometry-firmware/blob/main/docs/protocol.md);
  the reply is printed (`PARAM_VALUE kp = 0.03 (ok)`). Setting the read-only
  `control_rate_hz` or `protocol_version` is refused before anything is sent; range checks
  are left to the firmware, which answers `out of range`. Values set this way are not saved
  in flash on the firmware side.
- Before a velocity command, `send` asks the firmware for parameter 22 (`protocol_version`)
  and sends nothing that moves the motors unless the answer is 1, as the protocol document
  asks of a host. `--no-version-check` skips this. `stop` and parameter requests are always
  sent.
- `--hold S` repeats the command every 100 ms (five times per default watchdog period) and
  then sends a zero velocity; Ctrl-C during `send` also sends a zero velocity.
- `--dry-run` prints the frames without opening a port, `--log FILE.sslog` records both
  directions, `--seq N` sets the first sequence number.
- Exit status: 0 sent (and for parameters answered `ok`), 1 the firmware rejected a parameter
  request, 2 bad arguments or the port failed, 4 no `PARAM_VALUE` reply within `--wait`
  (default 1 s), 5 the protocol version was not confirmed, 130 interrupted.
- `send` opens the port itself, and Windows lets only one program hold a COM port: close
  `monitor` or the GUI first, or use the GUI's command panel, which sends on the connection
  it is already reading.

The frames are byte-for-byte those in the firmware's
[`test/vectors/protocol_vectors.json`](https://github.com/projectbin07-stack/encoder-odometry-firmware/blob/main/test/vectors/protocol_vectors.json)
(the test suite compares every velocity and parameter entry; these are the first ones, with
the vectors' sequence numbers):

```text
$ serial-scope send --dry-run --odometry vel 0.5 0 0
+    0.000s  > velocity vx=0.5 m/s vy=0 m/s wz=0 rad/s  seq 0  [03 01 01 02 0c 01 01 02 3f 01 01 01 01 01 01 01 03 73 0e 00]

$ serial-scope send --dry-run --seq 7 --odometry vel -0.25 0.125 1.5
+    0.000s  > velocity vx=-0.25 m/s vy=0.125 m/s wz=1.5 rad/s  seq 7  [05 01 01 07 0c 01 03 80 be 01 01 02 3e 01 05 c0 3f 7e dc 00]

$ serial-scope send --dry-run --seq 2 --odometry get-param kp
+    0.000s  > get kp  seq 2  [08 01 05 02 01 01 68 4a 00]

$ serial-scope send --dry-run --seq 3 --odometry set-param max_wheel_speed 12.5
+    0.000s  > set max_wheel_speed = 12.5  seq 3  [06 01 06 03 05 09 01 05 48 41 e5 9c 00]
```

Through pyserial's `loop://` device, which echoes every byte back, the decoder shows the
command that was sent; there is no firmware to answer, so the version check stops the
velocity command (exit 5) and a parameter request gets no reply (exit 4):

```text
$ serial-scope send loop:// --odometry vel 0.2 0 0.1 --hold 0.5
+    0.000s  > get protocol_version  seq 0  [03 01 05 05 01 16 de 46 00]
+    0.000s  PARAM_GET protocol_version
not sending a velocity command: no protocol_version reply within 1 s

$ serial-scope send loop:// --odometry vel 0.2 0 0.1 --hold 0.5 --no-version-check
+    0.000s  > velocity vx=0.2 m/s vy=0 m/s wz=0.1 rad/s  seq 0  [03 01 01 06 0c cd cc 4c 3e 01 01 01 07 cd cc cc 3d 19 41 00]
+    0.000s  VELOCITY_COMMAND vx=0.2 vy=0 wz=0.1
             (repeating every 0.1 s)
+    0.111s  VELOCITY_COMMAND vx=0.2 vy=0 wz=0.1
+    0.204s  VELOCITY_COMMAND vx=0.2 vy=0 wz=0.1
+    0.313s  VELOCITY_COMMAND vx=0.2 vy=0 wz=0.1
+    0.406s  VELOCITY_COMMAND vx=0.2 vy=0 wz=0.1
+    0.500s  > stop (zero velocity)  seq 5  [05 01 01 05 0c 01 01 01 01 01 01 01 01 01 01 01 03 e1 e4 00]
+    0.516s  VELOCITY_COMMAND vx=0 vy=0 wz=0
sent 6 frame(s); the protocol has no acknowledgement for velocity commands

$ serial-scope send loop:// --odometry get-param watchdog_ms
+    0.000s  > get watchdog_ms  seq 0  [03 01 05 05 01 0a 63 95 00]
+    0.000s  PARAM_GET watchdog_ms
no PARAM_VALUE reply within 1 s
```

```text
$ serial-scope send --help
usage: serial-scope send [-h] [-b BAUD] --odometry WORD [WORD ...] [--hold S]
                         [--wait S] [--seq SEQ] [--log FILE.sslog]
                         [--abs-time] [--dry-run] [--no-version-check]
                         [source]
...
  --odometry WORD [WORD ...]
                        vel VX VY WZ (m/s, m/s, rad/s) | stop | get-param NAME
                        | set-param NAME VALUE
  --hold S              vel only: repeat the command at 10 Hz for S seconds,
                        then send stop
  --wait S              read replies for S seconds after the last frame
                        (default 1 for parameters, 0.2 otherwise)
  --seq SEQ             sequence number of the first frame (default 0)
  --log FILE.sslog      record sent and received bytes
  --abs-time            print wall-clock times
  --dry-run             print the frames, send nothing
  --no-version-check    vel: do not ask for the firmware's protocol version
                        first
```

In the GUI the same commands are in a panel that appears under the toolbar while the
odometry decoder is in use (chosen by auto-detection or forced): vx / vy / wz fields with
*Send vel* and *Repeat 10 Hz* (keeps re-sending until STOP, a disconnect or a failed write),
a red **STOP** button that cancels the repeat and sends zero velocity, and a parameter picker
with *Get* and *Set*; replies appear next to it and in the errors list. The first *Send vel*
asks for the protocol version and is refused until the reply has arrived. The panel cannot
send to a replay.

![Odometry command panel above a replay of the synthetic odometry demo (dark theme, Stats tab)](docs/screenshot-odometry-commands-dark.png)

*Replay of synthetic data; nothing was sent.*

## GUI

`serial-scope gui` (options: `--port`, `-b`, `--replay LOG`, `--speed`, `--decode`,
`--theme light|dark`, `--buffer`).

- **Toolbar:** port picker with Refresh (USB VID:PID and chip hints; you can also type
  `tcp://host:port` or a pyserial URL), baud rate, decoder, Connect / Disconnect, Record...
  (raw `.sslog`), CSV... (decoded export), Replay... with speed, light / dark theme.
- **Plot:** the last N seconds of every channel; autoscale or a held range; Pause freezes the
  view while data keeps arriving; moving the mouse shows the nearest value of every visible
  channel; channels toggle on and off; markers from triggers and the Mark button are drawn as
  dashed lines; Snapshot writes the buffered points to a CSV.
- **Terminal:** received text (control characters made visible) or a hex dump, a send box
  with None / LF / CR / CR+LF line endings, C escapes (`\n`, `\x1b`) or hex input. For the
  binary formats (odometry, nrf24) it shows the decoded text (parameter replies, gateway log
  lines, key/value messages) instead of frame bytes; *Hex view* still shows the bytes.
- **Odometry commands:** the panel described [above](#sending-commands-to-encoder-odometry-firmware),
  shown only while the odometry decoder is in use.
- **Stats:** last, min, max, mean, rate and count per channel; bytes, samples, every error
  kind, sequence counters, radio link totals, notices, reconnects, markers and trigger
  counts.
- **Errors & triggers:** a time-stamped list of every decode error (red), device notice such
  as a radio link report (amber), trigger firing and command reply, and a box to add
  triggers.

The reader runs in its own thread and hands chunks to the UI through a queue; the UI drains it
every 30 ms (at most 2000 items per tick) and redraws the plot at most 15 times a second, so a
fast stream delays the display rather than freezing the window. The queue is unbounded on
purpose: if the UI falls behind, data waits instead of being dropped.

![Dark theme, Stats tab, replaying the synthetic ina219 demo log](docs/screenshot-ina219-dark.png)

![Replay of the synthetic nrf24 gateway log: odom.x, odom.y, imu.gz, link.crc_errors and link.unreliable_lost plotted over 30 s, with radio link notices, a USB CRC error and a trigger on the failed command in the Errors & triggers tab](docs/screenshot-nrf24-light.png)

*Synthetic nrf24 gateway demo log (`serial-scope demo --kind nrf24`) replayed at original
speed on Windows 11; only five of its channels are switched on.*

## Log format

`.sslog` is a small binary format designed to be replayed exactly:

```text
header  = "SSLOG" version:u8 start_ns:u64 meta_len:u32 meta:JSON
record  = kind:u8 dt_us:uvarint length:uvarint payload
kinds   = 1 received bytes, 2 sent bytes, 3 marker text, 4 status text
```

Payloads are stored unmodified, each record costs about three bytes of overhead, and every
record is flushed as it is written. A file cut short by a crash or a full disk is read up to its
last complete record and reported as truncated. With `--rotate-mb` the parts are `run.sslog`,
`run_002.sslog`, ... and are read back as one stream.

## Architecture

```text
             +-------------------+        +---------------------+
 COM / TCP / | Transport         |  read  | ReconnectingSource  |  status events
 replay /    | serial, tcp,      +------->+ open, detect drops, +----------------+
 fake        | replay, fake      |        | backoff, reopen     |                |
             +-------------------+        +----------+----------+                |
                                                     | Chunk(bytes, host time)   |
                         GUI: ReaderThread -> queue -> AppController.poll()       |
                         CLI: run_stream() loop                                   |
                                                     v                           v
                                          +----------+----------+
                                          | Session             |---> LogWriter (.sslog)
                                          |  Decoder (auto/...) |---> CsvExporter
                                          |  ChannelSet         |---> TriggerSet -> actions
                                          +----------+----------+
                                                     | events: TextLine, Sample,
                                                     | DecodeError, Notice, TriggerFired
                                                     v
                                  terminal / stats / plot (plotmodel -> Tk Canvas)
```

| Module | Responsibility |
| --- | --- |
| `transport.py`, `ports.py` | `Transport` interface; serial, TCP, replay and fake transports; port listing |
| `connection.py`, `reader.py` | reconnect state machine; background reader thread and queue |
| `decoders/` | `Decoder` base, line splitter and COBS frame splitter, the seven formats, auto-detection, registry |
| `commands.py` | encoder-odometry-firmware command frames: velocity, stop, parameter get / set |
| `channels.py` | ring buffers, running stats, rates, channel discovery |
| `logfile.py`, `csvexport.py` | `.sslog` writer / reader with rotation; decoded CSV and snapshots |
| `triggers.py`, `session.py` | trigger parsing and evaluation; the pipeline tying it together |
| `app.py`, `plotmodel.py`, `terminal.py` | GUI logic without Tk: controller, plot geometry, text and hex rendering |
| `gui.py` | the Tk widgets and their wiring only |
| `cli.py`, `demo.py` | command line; synthetic demo sessions |

### Adding a decoder

Line-based formats subclass `LineDecoder` and implement `decode_line`; `COBS(...) 00`
framed formats subclass `CobsFrameDecoder` and implement `decode_frame`; other binary formats
subclass `Decoder` and implement `feed`, keeping partial frames between calls. Report problems
with `self.error(...)` so they are counted, then register the class:

```python
from serial_scope.decoders import LineDecoder, register_decoder
from serial_scope.events import Event, Sample


class NmeaDepthDecoder(LineDecoder):
    name = "depth"
    description = "$SDDPT depth sentences"

    def decode_line(self, t: float, text: str, raw: bytes) -> list[Event]:
        if not text.startswith("$SDDPT,"):
            return [self.text(t, text)]
        try:
            depth = float(text.split(",")[1])
        except (IndexError, ValueError):
            return [self.text(t, text), self.error(t, "format", f"bad DPT: {text!r}", raw)]
        return [Sample(t, {"depth_m": depth}, source="DPT")]


register_decoder(NmeaDepthDecoder, auto_rank=35)  # omit auto_rank to exclude from auto
```

Test it the way the built-in decoders are tested: feed a fixture stream in chunk sizes down
to one byte, with garbage between records and corrupted checksums, and check the counters.

## Testing

```console
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest --cov
```

The suite (307 tests) covers:

- every decoder on fixture streams split at chunk sizes from 1 byte up, with garbage between
  frames, corrupted CRCs, overlong lines and frames, and auto-detection of every format
  (checked to produce exactly the same events as the explicit decoder);
- the odometry decoder and COBS / CRC-16 code against all vectors in the encoder-odometry
  shared vector file (messages, invalid frames, COBS edge cases);
- the nrf24 decoder against nrf24-telemetry-link's shared vectors (CRC, COBS, every message
  and invalid message, serial packets, gateway statistics), with reads split at chunk sizes
  from 1 byte up and corrupted, truncated, overlong, wrong-version, unknown-kind and
  short-body packets, plus link notices, gateway restarts and device-time wrap;
- the command encoder byte for byte against the velocity and parameter vectors of
  encoder-odometry-firmware, `send` against a scripted transport (replies, rejections,
  timeouts, `--hold`, Ctrl-C, the protocol-version check) and through `loop://`, and the GUI
  controller's command panel logic (repeat, stop, version check, failed writes);
- auto-detection between the two COBS formats, which share a packet layout: each decoder
  rejects the other's stream, and both are detected when joined mid-stream;
- the ina219 decoder
  against that project's frame fixtures and sample session, matching its own logger's
  summary (5 frames, 2 bad lines, 1 dropped, 1 restart);
- ring buffers, statistics against Python's `statistics` module, rates;
- `.sslog` round trips that are byte-exact, microsecond timestamps, truncation, rotation;
- trigger logic (hysteresis, edges, holdoff, regex, actions);
- reconnection with exponential backoff using the fake transport and a fake clock, and the
  reader thread delivering every chunk in order across a simulated unplug;
- the serial transport through pyserial's `loop://` device and the TCP transport against a
  local socket server;
- the CLI commands, and the GUI controller without Tk;
- smoke tests that build the real Tk window and replay the plotter, odometry and nrf24 demo
  logs through it (the odometry one checks that the command panel appears and refuses to
  send to a replay). They are skipped when no display is available; CI runs them under
  `xvfb-run` on Ubuntu and natively on Windows, and sets `SERIAL_SCOPE_REQUIRE_GUI=1` so
  they cannot silently skip there.

Coverage is measured without `gui.py`, the thin Tk view (widget construction and event
wiring); its logic lives in `app.py`, `plotmodel.py` and `terminal.py`, which are measured, and
`gui.py` itself is exercised by the smoke tests. Local coverage of the rest is 98 %.

## Hardware validation status

Stated plainly:

- **Not done:** serial-scope has **not been connected to a real board or USB-serial adapter**
  by its author. No Arduino, ESP32 or STM32 has been read, no cable has been unplugged under
  it, and the ESP32 TCP path has not talked to an ESP32. No nrf24-telemetry-link gateway
  has been read. **No command has been sent to encoder-odometry-firmware running on a
  board: the command path has never moved a motor.**
- **Done:** the serial transport has been exercised through pyserial's `loop://` device, the
  TCP transport against a local socket server, the reconnect logic with a scripted fake
  transport, the decoders against the firmware projects' reference vectors and fixtures
  (themselves not hardware captures), the command frames byte for byte against
  encoder-odometry-firmware's vectors and through `loop://`, and the GUI on Windows 11 by
  replaying synthetic logs (the screenshots) and in CI under Xvfb.
- **Therefore unverified:** behaviour of real USB-serial drivers on unplug and replug
  (Windows may report this as different exceptions or delays than the fake transport
  simulates), DTR/RTS auto-reset interaction, sustained throughput at high baud rates and how
  many channels the Canvas plot can keep up with, and the GUI on macOS. For commands:
  whether the firmware acts on these frames as intended on a real board (matching its own
  test vectors shows the bytes are what it expects, not how the robot behaves), and how
  evenly `--hold` and the GUI's repeat keep 100 ms spacing on a busy PC (a stall longer than
  the watchdog period stops the motors).

If you run it against a board, an issue describing the board, adapter, OS and what happened
(working or not) is very welcome, and this section will be updated.

## Known limitations

- Not hardware-validated (above).
- One port per window / process; no side-by-side comparison of two devices.
- The plot shows a sliding time window; there is no zoom or pan into older data (replay the
  log or open the CSV for that). Only the last `--buffer` points per channel are kept in
  memory; the log keeps everything.
- Auto-detection needs a few records. A stream that mixes formats (JSON and plotter lines from
  the same board) is decoded with whichever wins; force a decoder if it guesses wrong.
- The terminal does not interpret ANSI escape sequences; they are shown as `<ESC>[...`.
- Commands exist only for encoder-odometry-firmware (velocity, stop, parameters); there is
  no SET_POSE, and nothing is sent to the nrf24 gateway (its `send_command` and
  `request_stats` packets are decoded if seen, never generated).
- `send` cannot share a COM port with a running `monitor` or GUI on Windows; use the GUI's
  command panel to watch and command over one connection.
- The radio counters are the gateway's totals since its own boot; notices start with the
  second statistics packet serial-scope sees.
- The TCP transport is a plain client: no TLS, no authentication, no UDP.
- Snapshot and CSV timestamps are host times in Unix seconds; the GUI x axis is relative to
  the newest sample.

## Roadmap

- Try it on real boards (ESP32 CP210x and native USB, Nano CH340, STM32 Nucleo ST-LINK VCP) and
  record what happened here.
- Measure plot and decode throughput and publish the numbers with the method.
- Zoom and pan on paused data; XY plot (e.g. `odom.x` vs `odom.y`).
- nrf24 gateway commands (telemetry period, PA level, status request) and SET_POSE for
  encoder-odometry-firmware.
- A user plug-in directory for decoders without editing the package.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 Navaneeth KG

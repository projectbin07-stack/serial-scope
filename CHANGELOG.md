# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-19

### Added

- `nrf24` decoder for the USB packets of the nrf24-telemetry-link gateway (COBS + CRC-16,
  bodies up to 255 bytes) and the battery, odometry, IMU, key/value and command messages
  inside them, with channels in SI-style units. Auto-detection tells it apart from the
  encoder-odometry-firmware frames, which share its packet layout.
- Radio link quality: the gateway's statistics become `link.*` channels; the latest CRC
  error, malformed, duplicate, loss, retry, failure and resync totals appear in the CLI
  summary, `stats` and the GUI's Stats tab; increases are reported as a new kind of event,
  a *notice*, shown in the CLI (`~ link: ...`) and the GUI's errors list and matched by
  regex triggers. Notices do not count as decode errors.
- `serial-scope send SOURCE --odometry vel VX VY WZ | stop | get-param NAME |
  set-param NAME VALUE` for encoder-odometry-firmware, with `--hold` (repeat at 10 Hz, then
  stop), `--dry-run`, `--log` and a protocol-version check before anything that moves
  motors. The frames match the firmware's published vectors byte for byte.
- GUI command panel for encoder-odometry-firmware (velocity with optional 10 Hz repeat, a
  STOP button, parameter get / set), shown while the odometry decoder is in use.
- Synthetic `nrf24` demo log (`serial-scope demo --kind nrf24`) and a screenshot of it.
- README safety notes for velocity commands.

### Changed

- The terminal shows decoded text instead of frame bytes for the binary formats (hex view
  still shows the bytes).
- The COBS frame splitting and 32-bit clock unwrapping of the odometry decoder moved into a
  shared `CobsFrameDecoder` base.
- The Tk smoke tests share one Tk interpreter.

### Not done

- Nothing in this release has been run against real hardware; in particular the command
  path has never driven a motor.

## [0.1.0] - 2026-11-19

### Added

- Transports behind one interface: serial port through pyserial (also accepts pyserial URLs
  such as `loop://`), TCP client, replay of `.sslog` files at original or accelerated speed,
  and a scripted fake transport for tests.
- Automatic reconnection with exponential backoff when a port disappears or a socket closes.
- Port listing with USB VID:PID, description and a hint for common bridge chips and boards.
- Decoders: plain text, Arduino serial-plotter lines, CSV with a header, JSON lines, the
  ina219-battery-monitor `$BM1` CRC-8 lines and the encoder-odometry-firmware COBS +
  CRC-16 frames. Auto-detection that releases everything buffered during detection. CRC,
  framing and format errors are counted and reported, as are sequence gaps and restarts.
- Channel model with ring buffers, running min/max/mean, sample rate, and host or device
  time base.
- `.sslog` raw log format: every byte with microsecond host timestamps, sent bytes, markers
  and status notes, size-based rotation, and tolerant reading of truncated files. Decoded CSV
  export.
- Threshold, edge and regex triggers with mark, beep, stop-logging-after-N-seconds and
  snapshot actions.
- Tk GUI: port picker, baud and decoder selection, terminal with send box, line endings and
  hex view, multi-channel plot on a Canvas with autoscale, pause, cursor readout and channel
  toggles, stats and counters tables, error and trigger list, record / CSV / replay controls,
  light and dark themes.
- CLI: `list`, `monitor`, `replay`, `stats`, `decoders`, `demo`, `gui`.
- Synthetic demo logs for every supported format.

[0.2.0]: https://github.com/projectbin07-stack/serial-scope/releases/tag/v0.2.0
[0.1.0]: https://github.com/projectbin07-stack/serial-scope/releases/tag/v0.1.0

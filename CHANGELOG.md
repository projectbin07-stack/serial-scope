# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/projectbin07-stack/serial-scope/releases/tag/v0.1.0

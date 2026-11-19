# Test data provenance

These files are copied unchanged from the owner's firmware repositories so that
serial-scope's independent decoders are checked against the same reference data
as the firmware and its own host tools. serial-scope does not import those projects.

| File here | Copied from | Commit |
| --- | --- | --- |
| `odometry_protocol_vectors.json` | [encoder-odometry-firmware](https://github.com/projectbin07-stack/encoder-odometry-firmware) `test/vectors/protocol_vectors.json` | `4aef6807fb0fe916917df6aec60c649e27b2686a` |
| `nrf24_link_vectors.json` | [nrf24-telemetry-link](https://github.com/projectbin07-stack/nrf24-telemetry-link) `test/vectors/link_vectors.json` | `44148b5ac1360673368e36cd3fb7eecdf76d28d7` |
| `ina219_sample_session.log` | [ina219-battery-monitor](https://github.com/projectbin07-stack/ina219-battery-monitor) `tools/tests/data/sample_session.log` | `02b099f2674d1544d89a08a1da91688e6ba0144f` |

The `$BM1` frame strings used in `tests/test_decoder_ina219.py` are the shared fixtures
from ina219-battery-monitor's `test/test_telemetry/test_main.cpp` and
`tools/tests/test_logger.py` at the same commit.

`ina219_sample_session.log` is hand-written in its source repository (it deliberately
contains a truncated line, a corrupted checksum, a missing sequence number and a device
restart); it is not a capture from hardware.

`nrf24_link_vectors.json` is generated in its source repository by `nrflink vectors
--write`; serial-scope uses its `crc16`, `cobs`, `messages`, `invalid_messages`,
`serial_frames` and `gateway_stats` sections (the radio link-frame and hopping sections
describe the node-to-gateway radio layer, which never reaches the USB port).

The velocity-command and parameter get/set entries of `odometry_protocol_vectors.json`
are also what serial-scope's command encoder (`serial-scope send --odometry`) is checked
against, byte for byte.

If the protocols change upstream, copy the new files and update the commit column.

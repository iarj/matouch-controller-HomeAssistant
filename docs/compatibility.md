# Compatibility matrix

This matrix lists only combinations that have been validated. A version that is not listed is not supported, even if it might work.

| Integration | Home Assistant | Protocol | Schema | Templates | Display firmware |
|---|---|---|---|---|---|
| 0.1.0 | 2026.10.2 | 1.0 | 1.0 | Lights 1.1, Climate 2.0, Dehumidifier 2.0, Scene Launcher 1.0 | 2026-10-23_c2-identity |
| 0.1.0 | 2026.10.4 | 1.0 | 1.0 | Lights 1.1, Climate 2.0, Dehumidifier 2.0, Scene Launcher 1.0 | 2026-10-28_e1-4-install |
| 0.1.1 | 2026.10.4 | 1.0 | 1.0 | Lights 1.1, Climate 2.0, Dehumidifier 2.0, Scene Launcher 1.0 | 2026-10-28_e1-4-install |

## How to read it

- **Integration** is the `version` in the integration manifest. Each release is tagged `v<version>`.
- **Protocol** and **Schema** are the message and manifest versions the display reports. They are independent of the integration version.
- **Templates** are the page template versions validated with this combination. The integration rejects a display that reports a template version it does not support.
- **Display firmware** is the firmware version the display reports to Home Assistant.

HACS does not install the integration on a Home Assistant version older than the minimum declared in `hacs.json` (2026.10.2).
Version 0.1.1 changes only the version number and this documentation; its integration code is identical to 0.1.0.
New rows are added when another combination is validated.

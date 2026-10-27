# Known limitations

- **Validated combinations only.** Support is limited to the [compatibility matrix](compatibility.md).
- **One display validated.** The integration supports one config entry per display, but the current release has been validated with one display only.
- **Firmware is not distributed here.** The display ships with its firmware installed. This repository contains only the Home Assistant integration.
- **The display must be reachable to open the menu.** Opening the integration options or Reconfigure reads the runtime manifest from the display. If the display is offline, the form reports that it cannot connect.
- **Saved is not synchronized.** A change saved in Home Assistant reaches the display only when the display is online. Check the **Stare sincronizare MaTouch** entity of the ESPHome device.
- **Active pages and page order need the display online.** These values are stored on the display. If a write cannot be confirmed, the integration does not retry it automatically; open the menu again to read the current value.
- **Errors that Home Assistant does not report.** If an action fails without Home Assistant reporting an error, the display shows a timeout without a specific reason.
- **ESPHome device name changes.** If the ESPHome name of the display changes, the integration finds the display again by its unit identifier when Home Assistant restarts. If two candidates match, setup stops without changing anything. Run **Add integration** again for that display to select its ESPHome action.
- **Interface languages.** The integration interface is available in English and Romanian. The display uses its own built-in texts.

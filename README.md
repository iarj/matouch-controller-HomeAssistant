# MaTouch Controller for Home Assistant

`matouch_controller` is a Home Assistant custom integration for the MaTouch display: a round 2.1-inch touch display with a rotary knob.
The display ships with its pages already built in. This integration connects each page control to a Home Assistant entity and keeps the display in sync with Home Assistant.

The display connects to Home Assistant through ESPHome. The integration does not create copies of your entities. The mapped entities stay the source of truth.

## Requirements

- Home Assistant 2026.10.2. See the [compatibility matrix](docs/compatibility.md) for the validated combinations.
- [HACS](https://hacs.xyz/).
- A MaTouch display added to Home Assistant through the ESPHome integration, with **Allow the device to perform Home Assistant actions** turned on in the options of its ESPHome entry.

## Installation

1. In HACS, open the menu and select **Custom repositories**.
2. Add `https://github.com/iarj/matouch-controller-HomeAssistant` with the type **Integration**.
3. Find **MaTouch Controller** in HACS and download it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration** and select **MaTouch Controller**.

Updates appear in HACS. After an update, restart Home Assistant.

## Documentation

- [Usage guide](docs/usage.md): adding a display, configuring pages, active pages and page order, saved versus synchronized.
- [Compatibility matrix](docs/compatibility.md): validated versions.
- [Known limitations](docs/limitations.md).

The integration interface is available in English and Romanian.

## Support

Report problems in the [issue tracker](https://github.com/iarj/matouch-controller-HomeAssistant/issues). Include the Home Assistant version, the integration version and the display firmware version.

## License

The integration code and this documentation are released under the [MIT No Attribution (MIT-0)](LICENSE) license.

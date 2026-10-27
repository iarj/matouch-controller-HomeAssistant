# Usage guide

This guide describes how to add a MaTouch display to Home Assistant and how to configure its pages.
It applies to the versions in the [compatibility matrix](compatibility.md).

## Concepts

- **Display.** One MaTouch unit. Each display has its own config entry, identified by a stable unit identifier.
- **Functional page.** A page built into the display firmware, for example a lights page or a climate page. Each page uses one **template**, which defines its controls.
- **Mapping.** The link between one page control and one Home Assistant entity. Mappings are stored in Home Assistant, not in the display.
- **Configured page.** A page with all the mappings its template requires. A built-in page can stay not configured; this does not affect the other pages.
- **Active pages and page order.** Which functional pages the display shows, and in which order. The display stores both values itself.

In Home Assistant, each display appears as a **MaTouch** device. Each functional page appears as a child device of it. The display also has its own ESPHome device, which shows hardware values such as screen brightness, Wi-Fi signal and the synchronization state.

## Add a display

1. Make sure the display is online in the ESPHome integration and that **Allow the device to perform Home Assistant actions** is turned on in the options of its ESPHome entry.
2. Go to **Settings → Devices & services → Add integration → MaTouch Controller**.
3. Select the ESPHome action that returns the MaTouch runtime manifest. Its name ends in `matouch_get_manifest`.
4. Enter a device name (up to 20 characters) and confirm.

The display is added without configured pages. Configure the pages from the integration options.

If the selected display is already configured, no second entry is created. If only its ESPHome action changed, the existing entry keeps its mappings and uses the new action.

## Configure pages

Open the integration and select **Configure**. The MaTouch menu lists:

- every functional page built into the display: ✓ marks a configured page, ○ a page that is not configured;
- **Device name**;
- **Active pages · device**;
- **Page order · device**;
- **Done**.

Each confirmed change is saved immediately. The menu stays open after each save, so you can continue with another page. **Done** closes the menu without writing anything. Closing the dialog discards only the change you have not confirmed yet.

### Templates

| Template | What you configure | Entity domains | Display label |
|---|---|---|---|
| Lights 1.1 | Two slots, each saved on its own | `light`, `switch` | Required |
| Scene Launcher 1.0 | Four slots, each saved on its own | `scene`, `script` | Required |
| Climate 2.0 | One entity for the page | `climate` | Not used |
| Dehumidifier 2.0 | One entity for the page | `humidifier` | Not used |

- For a slot-based page, choose a slot to add, replace, rename or remove its mapping. Removing the last mapping leaves the page not configured.
- For a Climate or Dehumidifier page, select the entity or use **Remove this control mapping**.
- A display label has at most 8 characters: printable ASCII characters and the Romanian letters Ă, Â, Î, Ș, Ț in upper or lower case.
- An incomplete edit is never saved.

### Page headers and the device name

The device name belongs to the parent MaTouch device. Changing it does not change the configuration revision and does not resend the configuration to the display.

The header of a functional page follows the name of its child device. To change a page header, rename that child device in Home Assistant.

### Edit one mapping

Select **Reconfigure** on the integration entry, then a page and a control. Only that mapping changes. The other mappings stay as they are.

## Active pages and page order

**Active pages · device** and **Page order · device** change values stored on the display. They do not change mappings or the configuration revision.

- At least one functional page must stay active. An inactive page keeps its mappings.
- For the page order, enter every page slot exactly once, separated by commas, for example `2, 1, 3, 4`.
- The display must be online. Home Assistant writes the value and reads it back. If the result cannot be confirmed, the integration does not retry the write automatically.

## Saved versus synchronized

Saving in Home Assistant and synchronizing the display are separate steps.

- After each save, the menu shows the new configuration revision.
- The **Runtime configuration** sensor of the MaTouch device shows the saved revision.
- The **Stare sincronizare MaTouch** entity of the ESPHome device shows the state reported by the display. It is unavailable while the display is offline.

When the display is online, it synchronizes automatically. A change saved while the display is offline is sent when the display reconnects.

## When a form reports that the configuration changed

If the configuration changes in another window while a form is open, the form stops with **Configuration changed. Reopen the form.** Nothing is written by that form. Changes that were already saved are kept. Open the form again to continue.

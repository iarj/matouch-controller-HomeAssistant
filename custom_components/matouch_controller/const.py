"""Constants for the MaTouch Controller integration."""

DOMAIN = "matouch_controller"

PROTOCOL_VERSION = "1.0"
SCHEMA_VERSION = "1.0"

CONF_DEVICE_UID = "device_uid"
CONF_DISPLAY_NAME = "display_name"
CONF_MANIFEST_ACTION = "manifest_action"
CONF_CONFIG_REVISION = "config_revision"
CONF_VALIDATED_CONFIG_HASH = "validated_config_hash"
CONF_MAPPINGS = "mappings"
CONF_PAGE_SLOT = "page_slot"
CONF_CONTROL_SLOT = "control_slot"
CONF_LABEL = "label"
CONF_REMOVE_MAPPING = "remove_mapping"

DEFAULT_DISPLAY_NAME = "MaTouch Controller"
DISPLAY_NAME_MAX_LENGTH = 20

RUNTIME_LABEL_REQUIRED = True
RUNTIME_LABEL_MAX_LENGTH = 8


def page_device_identifier(device_uid: str, page_slot: int) -> str:
    """Return the integration-local child-device identifier for one page."""
    return f"{device_uid}:page:{page_slot}"

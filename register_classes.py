
from labscript_devices import register_classes

register_classes(
    'KinetixCamera',
    BLACS_tab='user_devices.Cesium.KinetixCamera.blacs_tabs.KinetixCameraTab',
    runviewer_parser=None,
)

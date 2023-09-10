from labscript_devices.IMAQdxCamera.blacs_tabs import IMAQdxCameraTab

class KinetixCameraTab(IMAQdxCameraTab):

    worker_class = 'user_devices.Cesium.KinetixCamera.blacs_workers.KinetixCameraWorker'
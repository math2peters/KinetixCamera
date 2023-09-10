from pyvcam import pvc 
from pyvcam.camera import Camera   
import sys
from time import perf_counter
from blacs.tab_base_classes import Worker
import threading
import numpy as np
from labscript_utils import dedent
import labscript_utils.h5_lock
import h5py
import labscript_utils.properties
import zmq

from labscript_utils.ls_zprocess import Context
from labscript_utils.shared_drive import path_to_local
from labscript_utils.properties import set_attributes


class KinetixCamera(object):
    def __init__(self, serial_number):


        # Find the camera:
        print("Finding camera...")
        pvc.init_pvcam()                   # Initialize PVCAM 
        print("Found cameras: {}".format(Camera.get_available_camera_names()))
        self.camera = Camera.select_camera(serial_number)

        # Connect to the camera:
        print("Connecting to camera...")
        self.camera.open()      
        # Keep an img attribute so we don't have to create it every time
        self.exception_on_failed_shot = True
        self._abort_acquisition = False
        self.img = self.camera.get_frame()

    def set_attributes(self, attr_dict):
        for k, v in attr_dict.items():
            self.set_attribute(k, v)

    def set_attribute(self, name, value):
        """Set the value of the attribute of the given name to the given value"""
        _value = value  # Keep the original for the sake of the error message
        if isinstance(_value, str):
            _value = _value.encode('utf8')
        try:
            if name == 'roi':
                self.camera.set_roi(*value)
            else:
                setattr(self.camera, name, value)
        except Exception as e:
            # Add some info to the exception:
            msg = f"failed to set attribute {name} to {value}"
            raise Exception(msg) from e
        
    def get_attribute_dict(self):
        return self.camera.__dict__

    def get_attribute(self, name):
        """Return current value of attribute of the given name"""
        try:
            value = self.camera.get_param(name)
            return value
        except Exception as e:
            # Add some info to the exception:
            raise Exception(f"Failed to get attribute {name}") from e

    def snap(self):
        """Acquire a single image and return it"""

        image = self.camera.get_frame()
        return image
    

    def grab_multiple(self, n_images, images):
        print(f"Attempting to grab {n_images} images.")

        # images_temp = self.camera.get_sequence(n_images)
        # [images.append(images_temp[i]) for i in range(n_images)]
        images.append(self.camera.get_frame())
                    
        print(f"Got {len(images)} of {n_images} images.")
        return images

    def close(self):
        self.camera.close()


class KinetixCameraWorker(Worker):
    # Subclasses may override this if their interface class takes only the serial number
    # as an instantiation argument, otherwise they may reimplement get_camera():
    interface_class = KinetixCamera

    def init(self):
        self.camera = self.get_camera()
        print("Setting attributes...")
        self.smart_cache = {}
        self.set_attributes_smart(self.camera_attributes)
        self.set_attributes_smart(self.manual_mode_camera_attributes)
        print("Initialisation complete")
        self.images = None
        self.n_images = None
        self.attributes_to_save = None
        self.exposures = None
        self.acquisition_thread = None
        self.h5_filepath = None
        self.stop_acquisition_timeout = None
        self.exception_on_failed_shot = None
        self.continuous_stop = threading.Event()
        self.continuous_thread = None
        self.continuous_dt = None
        self.image_socket = Context().socket(zmq.REQ)
        self.image_socket.connect(
            f'tcp://{self.parent_host}:{self.image_receiver_port}'
        )

    def get_camera(self):
        """Return an instance of the camera interface class. Subclasses may override
        this method to pass required arguments to their class if they require more
        than just the serial number."""

        return self.interface_class(self.serial_number)

    def set_attributes_smart(self, attributes):
        """Call self.camera.set_attributes() to set the given attributes, only setting
        those that differ from their value in, or are absent from self.smart_cache.
        Update self.smart_cache with the newly-set values"""
        uncached_attributes = {}
        for name, value in attributes.items():
            if name not in self.smart_cache or self.smart_cache[name] != value:
                uncached_attributes[name] = value
                self.smart_cache[name] = value
        self.camera.set_attributes(uncached_attributes)

    def get_attributes_as_dict(self):
        """Return a dict of the attributes of the camera for the given visibility
        level"""
        attributes_dict = self.camera.get_attribute_dict()

        return attributes_dict

    def get_attributes_as_text(self, visibility_level):
        """Return a string representation of the attributes of the camera for
        the given visibility level"""
        attrs = self.get_attributes_as_dict()
        # Format it nicely:
        lines = [f'    {str(key)}: {str(value)},' for key, value in attrs.items()]
        dict_repr = '\n'.join(['{'] + lines + ['}'])
        return self.device_name + '_camera_attributes = ' + dict_repr

    def snap(self):
        """Acquire one frame in manual mode. Send it to the parent via
        self.image_socket. Wait for a response from the parent."""
        image = self.camera.snap()
        self._send_image_to_parent(image)

    def _send_image_to_parent(self, image):
        """Send the image to the GUI to display. This will block if the parent process
        is lagging behind in displaying frames, in order to avoid a backlog."""
        metadata = dict(dtype=str(image.dtype), shape=image.shape)
        self.image_socket.send_json(metadata, zmq.SNDMORE)
        self.image_socket.send(image, copy=False)
        response = self.image_socket.recv()
        assert response == b'ok', response

    def continuous_loop(self, dt):
        """Acquire continuously in a loop, with minimum repetition interval dt"""
        while True:
            if dt is not None:
                t = perf_counter()
            image = self.camera.snap()
            self._send_image_to_parent(image)
            if dt is None:
                timeout = 0
            else:
                timeout = t + dt - perf_counter()
            if self.continuous_stop.wait(timeout):
                self.continuous_stop.clear()
                break

    def start_continuous(self, dt):
        """Begin continuous acquisition in a thread with minimum repetition interval
        dt"""
        assert self.continuous_thread is None
        self.continuous_thread = threading.Thread(
            target=self.continuous_loop, args=(dt,), daemon=True
        )
        self.continuous_thread.start()
        self.continuous_dt = dt

    def stop_continuous(self, pause=False):
        """Stop the continuous acquisition thread"""
        assert self.continuous_thread is not None
        self.continuous_stop.set()
        self.continuous_thread.join()
        self.continuous_thread = None
        # If we're just 'pausing', then do not clear self.continuous_dt. That way
        # continuous acquisition can be resumed with the same interval by calling
        # start(self.continuous_dt), without having to get the interval from the parent
        # again, and the fact that self.continuous_dt is not None can be used to infer
        # that continuous acquisiton is paused and should be resumed after a buffered
        # run is complete:
        if not pause:
            self.continuous_dt = None

    def transition_to_buffered(self, device_name, h5_filepath, initial_values, fresh):
        if getattr(self, 'is_remote', False):
            h5_filepath = path_to_local(h5_filepath)
        if self.continuous_thread is not None:
            # Pause continuous acquistion during transition_to_buffered:
            self.stop_continuous(pause=True)
        with h5py.File(h5_filepath, 'r') as f:
            group = f['devices'][self.device_name]
            if not 'EXPOSURES' in group:
                return {}
            self.h5_filepath = h5_filepath
            self.exposures = group['EXPOSURES'][:]
            self.n_images = len(self.exposures)

            # Get the camera_attributes from the device_properties
            properties = labscript_utils.properties.get(
                f, self.device_name, 'device_properties'
            )
            camera_attributes = properties['camera_attributes']
            self.stop_acquisition_timeout = properties['stop_acquisition_timeout']
            self.exception_on_failed_shot = properties['exception_on_failed_shot']
            #saved_attr_level = properties['saved_attribute_visibility_level']
            self.camera.exception_on_failed_shot = self.exception_on_failed_shot
        # Only reprogram attributes that differ from those last programmed in, or all of
        # them if a fresh reprogramming was requested:
        if fresh:
            self.smart_cache = {}
        self.set_attributes_smart(camera_attributes)
        # Get the camera attributes, so that we can save them to the H5 file:

        self.attributes_to_save = self.get_attributes_as_dict()

        print(f"Configuring camera for {self.n_images} images.")
        self.images = []
        self.acquisition_thread = threading.Thread(
            target=self.camera.grab_multiple,
            args=(self.n_images, self.images),
            daemon=True,
        )
        self.acquisition_thread.start()
        return {}

    def transition_to_manual(self):
        if self.h5_filepath is None:
            print('No camera exposures in this shot.\n')
            return True
        assert self.acquisition_thread is not None
        self.acquisition_thread.join(timeout=self.stop_acquisition_timeout)
        if self.acquisition_thread.is_alive():
            msg = """Acquisition thread did not finish. Likely did not acquire expected
                number of images. Check triggering is connected/configured correctly"""
            if self.exception_on_failed_shot:
                self.abort()
                raise RuntimeError(dedent(msg))
            else:
                self.acquisition_thread.join()
                print(dedent(msg), file=sys.stderr)
        self.acquisition_thread = None

        print("Stopping acquisition.")

        print(f"Saving {len(self.images)}/{len(self.exposures)} images.")

        with h5py.File(self.h5_filepath, 'r+') as f:
            # Use orientation for image path, device_name if orientation unspecified
            if self.orientation is not None:
                image_path = 'images/' + self.orientation
            else:
                image_path = 'images/' + self.device_name
            image_group = f.require_group(image_path)
            image_group.attrs['camera'] = self.device_name


            # Whether we failed to get all the expected exposures:
            image_group.attrs['failed_shot'] = len(self.images) != len(self.exposures)

            # key the images by name and frametype. Allow for the case of there being
            # multiple images with the same name and frametype. In this case we will
            # save an array of images in a single dataset.
            images = {
                (exposure['name'], exposure['frametype']): []
                for exposure in self.exposures
            }

            # Iterate over expected exposures, sorted by acquisition time, to match them
            # up with the acquired images:
            self.exposures.sort(order='t')
            for image, exposure in zip(self.images, self.exposures):
                images[(exposure['name'], exposure['frametype'])].append(image)

            # Save images to the HDF5 file:
            for (name, frametype), imagelist in images.items():
                data = imagelist[0] if len(imagelist) == 1 else np.array(imagelist)
                print(f"Saving frame(s) {name}/{frametype}.")
                group = image_group.require_group(name)
                dset = group.create_dataset(
                    frametype, data=data, dtype='uint16', compression='gzip'
                )
                # Specify this dataset should be viewed as an image
                dset.attrs['CLASS'] = np.string_('IMAGE')
                dset.attrs['IMAGE_VERSION'] = np.string_('1.2')
                dset.attrs['IMAGE_SUBCLASS'] = np.string_('IMAGE_GRAYSCALE')
                dset.attrs['IMAGE_WHITE_IS_ZERO'] = np.uint8(0)

        # If the images are all the same shape, send them to the GUI for display:
        try:
            image_block = np.stack(self.images)
        except ValueError:
            print("Cannot display images in the GUI, they are not all the same shape")
        else:
            self._send_image_to_parent(image_block)

        self.images = None
        self.n_images = None
        self.attributes_to_save = None
        self.exposures = None
        self.h5_filepath = None
        self.stop_acquisition_timeout = None
        self.exception_on_failed_shot = None
        print("Setting manual mode camera attributes.\n")
        self.set_attributes_smart(self.manual_mode_camera_attributes)
        if self.continuous_dt is not None:
            # If continuous manual mode acquisition was in progress before the bufferd
            # run, resume it:
            self.start_continuous(self.continuous_dt)
        return True

    def abort(self):
        if self.acquisition_thread is not None:
            self.acquisition_thread.join()
            self.acquisition_thread = None
        self.camera._abort_acquisition = False
        self.images = None
        self.n_images = None
        self.attributes_to_save = None
        self.exposures = None
        self.acquisition_thread = None
        self.h5_filepath = None
        self.stop_acquisition_timeout = None
        self.exception_on_failed_shot = None
        # Resume continuous acquisition, if any:
        if self.continuous_dt is not None and self.continuous_thread is None:
            self.start_continuous(self.continuous_dt)
        return True

    def abort_buffered(self):
        return self.abort()

    def abort_transition_to_buffered(self):
        return self.abort()

    def program_manual(self, values):
        return {}

    def shutdown(self):
        if self.continuous_thread is not None:
            self.stop_continuous()
        self.camera.close()
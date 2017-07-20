"""
    Contains classes related to display of data items.
"""

# standard libraries
import collections
import copy
import functools
import gettext
import math
import numbers
import operator
import threading
import typing
import uuid
import weakref

import numpy

from nion.data import Calibration
from nion.data import Core
from nion.data import DataAndMetadata
from nion.data import Image
from nion.swift.model import Cache
from nion.swift.model import ColorMaps
from nion.swift.model import Graphics
from nion.utils import Event
from nion.utils import Observable
from nion.utils import Persistence


_ = gettext.gettext


class GraphicSelection:
    def __init__(self, indexes=None):
        super(GraphicSelection, self).__init__()
        self.__indexes = copy.copy(indexes) if indexes else set()
        self.changed_event = Event.Event()

    def __copy__(self):
        return type(self)(self.__indexes)

    def __eq__(self, other):
        return other is not None and self.indexes == other.indexes

    def __ne__(self, other):
        return other is None or self.indexes != other.indexes

    # manage selection
    @property
    def current_index(self):
        if len(self.__indexes) == 1:
            for index in self.__indexes:
                return index
        return None

    @property
    def has_selection(self):
        return len(self.__indexes) > 0

    def contains(self, index):
        return index in self.__indexes

    @property
    def indexes(self):
        return self.__indexes

    def clear(self):
        old_index = self.__indexes.copy()
        self.__indexes = set()
        if old_index != self.__indexes:
            self.changed_event.fire()

    def add(self, index):
        assert isinstance(index, numbers.Integral)
        old_index = self.__indexes.copy()
        self.__indexes.add(index)
        if old_index != self.__indexes:
            self.changed_event.fire()

    def remove(self, index):
        assert isinstance(index, numbers.Integral)
        old_index = self.__indexes.copy()
        self.__indexes.remove(index)
        if old_index != self.__indexes:
            self.changed_event.fire()

    def add_range(self, range):
        for index in range:
            self.add(index)

    def set(self, index):
        assert isinstance(index, numbers.Integral)
        old_index = self.__indexes.copy()
        self.__indexes = set()
        self.__indexes.add(index)
        if old_index != self.__indexes:
            self.changed_event.fire()

    def toggle(self, index):
        assert isinstance(index, numbers.Integral)
        old_index = self.__indexes.copy()
        if index in self.__indexes:
            self.__indexes.remove(index)
        else:
            self.__indexes.add(index)
        if old_index != self.__indexes:
            self.changed_event.fire()

    def insert_index(self, new_index):
        new_indexes = set()
        for index in self.__indexes:
            if index < new_index:
                new_indexes.add(index)
            else:
                new_indexes.add(index + 1)
        if self.__indexes != new_indexes:
            self.__indexes = new_indexes
            self.changed_event.fire()

    def remove_index(self, remove_index):
        new_indexes = set()
        for index in self.__indexes:
            if index != remove_index:
                if index > remove_index:
                    new_indexes.add(index - 1)
                else:
                    new_indexes.add(index)
        if self.__indexes != new_indexes:
            self.__indexes = new_indexes
            self.changed_event.fire()


def calculate_display_range(display_limits, data_range, data_sample, xdata, complex_display_type):
    if display_limits is not None:
        display_limit_low = display_limits[0] if display_limits[0] is not None else data_range[0]
        display_limit_high = display_limits[1] if display_limits[1] is not None else data_range[1]
        return display_limit_low, display_limit_high
    if xdata and xdata.is_data_complex_type and complex_display_type is None:  # log absolute
        if data_sample is not None:
            fraction = 0.05
            display_limit_low = data_sample[int(data_sample.shape[0] * fraction)]
            display_limit_high = data_range[1]
            return display_limit_low, display_limit_high
    return data_range


DisplayValues = collections.namedtuple("DisplayValues", ("display_data_and_metadata", "data_range", "data_sample", "display_range", "display_rgba", "display_rgba_timestamp"))

class CalculatedDisplayValues:

    def __init__(self):
        # parent
        self.__parent = None

        # the calculated display values
        self.__display_data_and_metadata = None
        self.__data_range = None
        self.__data_sample = None
        self.__display_range = None
        self.__display_rgba = None
        self.__display_rgba_timestamp = None

        self.__display_data_dirty = False
        self.__display_range_dirty = False
        self.__display_rgba_dirty = False

        # the inputs
        self.__data_and_metadata = None
        self.__sequence_index = None
        self.__collection_index = None
        self.__slice_center = None
        self.__slice_width = None
        self.__complex_display_type = None
        self.__display_limits = None
        self.__color_map_data = None

        # a lock for controlling access to the dirty flags
        # a race condition happens if a dirty flag gets set during
        # copying or clearing the dirty flags.
        self.__lock = threading.RLock()

    def _set_parent(self, parent: "CalculatedDisplayValues") -> None:
        self.__parent = parent

    def copy_and_calculate(self) -> "CalculatedDisplayValues":
        with self.__lock:
            calculated_display_values = copy.copy(self)
            self.__display_data_dirty = False
            self.__display_range_dirty = False
            self.__display_rgba_dirty = False
        calculated_display_values._set_parent(self)
        calculated_display_values.__calculate()
        return calculated_display_values

    def values(self) -> DisplayValues:
        return DisplayValues(self.__display_data_and_metadata, self.__data_range, self.__data_sample, self.__display_range, self.__display_rgba, self.__display_rgba_timestamp)

    def _get_display_data_and_metadata(self):
        return self.__display_data_and_metadata

    def _get_display_rgba(self):
        return self.__display_rgba

    def _get_display_rgba_timestamp(self):
        return self.__display_rgba_timestamp

    def _set_data_and_metadata(self, value):
        self.__data_and_metadata = value
        with self.__lock:
            self.__display_data_dirty = True
            self.__display_range_dirty = True
            self.__display_rgba_dirty = True

    def _set_sequence_index(self, value):
        self.__sequence_index = value
        with self.__lock:
            self.__display_data_dirty = True
            self.__display_range_dirty = True
            self.__display_rgba_dirty = True

    def _set_collection_index(self, value):
        self.__collection_index = value
        with self.__lock:
            self.__display_data_dirty = True
            self.__display_range_dirty = True
            self.__display_rgba_dirty = True

    def _set_slice_center(self, value):
        self.__slice_center = value
        with self.__lock:
            self.__display_data_dirty = True
            self.__display_range_dirty = True
            self.__display_rgba_dirty = True

    def _set_slice_width(self, value):
        self.__slice_width = value
        with self.__lock:
            self.__display_data_dirty = True
            self.__display_range_dirty = True
            self.__display_rgba_dirty = True

    def _set_complex_display_type(self, value):
        self.__complex_display_type = value
        with self.__lock:
            self.__display_data_dirty = True
            self.__display_range_dirty = True
            self.__display_rgba_dirty = True

    def _set_display_limits(self, value):
        self.__display_limits = value
        with self.__lock:
            self.__display_range_dirty = True
            self.__display_rgba_dirty = True

    def _set_color_map_data(self, value):
        self.__color_map_data = value
        with self.__lock:
            self.__display_rgba_dirty = True

    def __calculate(self):
        if self.__display_data_dirty:
            self.__display_data_and_metadata = self.__calculate_display_data_and_metadata()
            self.__data_range = self.__calculate_data_range()
            self.__data_sample = self.__calculate_data_sample()
        if self.__display_range_dirty:
            self.__display_range = self.__calculate_display_range()
        if self.__display_rgba_dirty:
            self.__display_rgba, self.__display_rgba_timestamp = self.__calculate_display_rgba()
        if self.__parent:
            self.__parent.update_values(self.values())

    def __calculate_display_data_and_metadata(self):
        data_and_metadata = self.__data_and_metadata
        if data_and_metadata is not None:
            timestamp = data_and_metadata.timestamp
            data_and_metadata = Core.function_display_data(data_and_metadata, self.__sequence_index, self.__collection_index, self.__slice_center, self.__slice_width, self.__complex_display_type)
            if data_and_metadata:
                data_and_metadata.data_metadata.timestamp = timestamp
            return data_and_metadata
        return None

    def __calculate_data_range(self):
        display_data_and_metadata = self.__display_data_and_metadata
        display_data = display_data_and_metadata.data if display_data_and_metadata else None
        if display_data is not None and display_data.size and self.__data_and_metadata:
            data_shape = self.__data_and_metadata.data_shape
            data_dtype = self.__data_and_metadata.data_dtype
            if Image.is_shape_and_dtype_rgb_type(data_shape, data_dtype):
                data_range = (0, 255)
            elif Image.is_shape_and_dtype_complex_type(data_shape, data_dtype):
                data_range = (numpy.amin(display_data), numpy.amax(display_data))
            else:
                data_range = (numpy.amin(display_data), numpy.amax(display_data))
        else:
            data_range = None
        if data_range is not None:
            if math.isnan(data_range[0]) or math.isnan(data_range[1]) or math.isinf(data_range[0]) or math.isinf(data_range[1]):
                data_range = (0.0, 0.0)
        return data_range

    def __calculate_data_sample(self):
        display_data_and_metadata = self.__display_data_and_metadata
        display_data = display_data_and_metadata.data if display_data_and_metadata else None
        if display_data is not None and display_data.size and self.__data_and_metadata:
            data_shape = self.__data_and_metadata.data_shape
            data_dtype = self.__data_and_metadata.data_dtype
            if Image.is_shape_and_dtype_rgb_type(data_shape, data_dtype):
                data_sample = None
            elif Image.is_shape_and_dtype_complex_type(data_shape, data_dtype):
                data_sample = numpy.sort(numpy.random.choice(display_data.reshape(numpy.product(display_data.shape)), 200))
            else:
                data_sample = None
        else:
            data_sample = None
        return data_sample

    def __calculate_display_range(self):
        data_range = self.__data_range
        data_sample = self.__data_sample
        return calculate_display_range(self.__display_limits, data_range, data_sample, self.__data_and_metadata, self.__complex_display_type)

    def __calculate_display_rgba(self):
        display_data_and_metadata = self.__display_data_and_metadata
        if display_data_and_metadata is not None and self.__data_and_metadata is not None:
            # display_range is just display_limits but calculated if display_limits is None
            data_range = self.__data_range
            data_sample = self.__data_sample
            if data_range is not None:  # workaround until validating and retrieving data stats is an atomic operation
                display_range = calculate_display_range(self.__display_limits, data_range, data_sample, self.__data_and_metadata, self.__complex_display_type)
                return Core.function_display_rgba(display_data_and_metadata, display_range, self.__color_map_data).data, display_data_and_metadata.timestamp
        return None, None

    def update_values(self, values):
        self.__display_data_and_metadata = values.display_data_and_metadata
        self.__data_range = values.data_range
        self.__data_sample = values.data_sample
        self.__display_range = values.display_range
        self.__display_rgba = values.display_rgba
        self.__display_rgba_timestamp = values.display_rgba_timestamp


class Display(Observable.Observable, Persistence.PersistentObject):
    """Display information for a DataItem.

    Also handles conversion of raw data to formats suitable for display such as raster RGBA.
    """

    def __init__(self):
        super().__init__()
        self.__container_weak_ref = None
        self.__cache = Cache.ShadowCache()
        self.__color_map_data = None
        self.define_property("display_type", changed=self.__display_type_changed)
        self.define_property("complex_display_type", changed=self.__property_changed)
        self.define_property("display_calibrated_values", True, changed=self.__property_changed)
        self.define_property("dimensional_calibration_style", None, changed=self.__property_changed)
        self.define_property("display_limits", validate=self.__validate_display_limits, changed=self.__property_changed)
        self.define_property("y_min", changed=self.__property_changed)
        self.define_property("y_max", changed=self.__property_changed)
        self.define_property("y_style", "linear", changed=self.__property_changed)
        self.define_property("left_channel", changed=self.__property_changed)
        self.define_property("right_channel", changed=self.__property_changed)
        self.define_property("legend_labels", changed=self.__property_changed)
        self.define_property("sequence_index", 0, validate=self.__validate_sequence_index, changed=self.__property_changed)
        self.define_property("collection_index", (0, 0, 0), validate=self.__validate_collection_index, changed=self.__property_changed)
        self.define_property("slice_center", 0, validate=self.__validate_slice_center, changed=self.__slice_interval_changed)
        self.define_property("slice_width", 1, validate=self.__validate_slice_width, changed=self.__slice_interval_changed)
        self.define_property("color_map_id", changed=self.__color_map_id_changed)
        self.define_relationship("graphics", Graphics.factory, insert=self.__insert_graphic, remove=self.__remove_graphic)

        self.will_close_event = Event.Event()  # for shutting down thumbnails; hopefully temporary.

        self.__calculated_display_values = CalculatedDisplayValues()
        self.__calculated_display_values_available_event = Event.Event()
        self.__calculated_display_values_lock = threading.RLock()  # lock for display values pending flag
        self.__calculated_display_values_thread = None
        self.__calculated_display_values_thread_lock = threading.RLock()  # lock for starting and stopping thread
        self.__calculated_display_values_pending = False

        self._calculated_display_values_test_exception = False  # used for testing

        self.__calculated_display_values._set_data_and_metadata(None)
        self.__calculated_display_values._set_sequence_index(self.sequence_index)
        self.__calculated_display_values._set_collection_index(self.collection_index)
        self.__calculated_display_values._set_slice_center(self.slice_center)
        self.__calculated_display_values._set_slice_width(self.slice_width)
        self.__calculated_display_values._set_complex_display_type(self.complex_display_type)
        self.__calculated_display_values._set_display_limits(self.display_limits)
        self.__calculated_display_values._set_color_map_data(self.__color_map_data)

        self.__graphics_map = dict()  # type: typing.MutableMapping[uuid.UUID, Graphics.Graphic]
        self.__graphic_changed_listeners = list()
        self.__data_and_metadata = None  # the most recent data to be displayed. should have immediate data available.
        self.graphic_selection = GraphicSelection()

        def graphic_selection_changed():
            # relay the message
            self.display_graphic_selection_changed_event.fire(self.graphic_selection)

        self.__graphic_selection_changed_event_listener = self.graphic_selection.changed_event.listen(graphic_selection_changed)
        self.about_to_be_removed_event = Event.Event()
        self.display_changed_event = Event.Event()
        self.display_data_will_change_event = Event.Event()
        self.display_type_changed_event = Event.Event()
        self.display_graphic_selection_changed_event = Event.Event()
        self._about_to_be_removed = False
        self.__calculated_display_values_thread_ok = True
        self._closed = False

    def close(self):
        self.will_close_event.fire()
        with self.__calculated_display_values_thread_lock:
            self.__calculated_display_values_thread_ok = False
            if self.__calculated_display_values_thread:
                self.__calculated_display_values_thread.join()
                self.__calculated_display_values_thread = None
        self.__graphic_selection_changed_event_listener.close()
        self.__graphic_selection_changed_event_listener = None
        for graphic in copy.copy(self.graphics):
            self.__disconnect_graphic(graphic, 0)
            graphic.close()
        self.graphic_selection = None
        assert self._about_to_be_removed
        assert not self._closed
        self._closed = True
        self.__container_weak_ref = None

    def read_from_dict(self, properties):
        super().read_from_dict(properties)
        if self.dimensional_calibration_style is None:
            self._get_persistent_property("dimensional_calibration_style").value = "calibrated" if self.display_calibrated_values else "relative-top-left"

    @property
    def container(self):
        return self.__container_weak_ref()

    def about_to_be_inserted(self, container):
        assert self.__container_weak_ref is None
        self.__container_weak_ref = weakref.ref(container)

    def about_to_be_removed(self):
        # called before close and before item is removed from its container
        for graphic in self.graphics:
            graphic.about_to_be_removed()
        self.about_to_be_removed_event.fire()
        assert not self._about_to_be_removed
        self._about_to_be_removed = True

    def insert_model_item(self, container, name, before_index, item):
        if self.__container_weak_ref:
            self.container.insert_model_item(container, name, before_index, item)
        else:
            container.insert_item(name, before_index, item)

    def remove_model_item(self, container, name, item):
        if self.__container_weak_ref:
            self.container.remove_model_item(container, name, item)
        else:
            container.remove_item(name, item)

    def clone(self) -> "Display":
        display = Display()
        display.uuid = self.uuid
        for graphic in self.graphics:
            display.add_graphic(graphic.clone())
        return display

    @property
    def _display_cache(self):
        return self.__cache

    @property
    def data_and_metadata_for_display_panel(self):
        return self.__data_and_metadata

    def reset_display_limits(self):
        """Reset display limits so that they are auto calculated whenever the data changes."""
        self.display_limits = None

    def auto_display_limits(self):
        """Calculate best display limits and set them."""
        display_data_and_metadata = self.get_calculated_display_values(True).display_data_and_metadata
        data = display_data_and_metadata.data if display_data_and_metadata else None
        if data is not None:
            percentiles = numpy.nanpercentile(data.flatten(), (0.1, 99.9))
            range = percentiles[1] - percentiles[0]
            self.display_limits = percentiles[0] - range * 0.1, percentiles[1] + range * 0.1

    def view_to_intervals(self, data_and_metadata: DataAndMetadata.DataAndMetadata, intervals: typing.List[typing.Tuple[float, float]]) -> None:
        """Change the view to encompass the channels and data represented by the given intervals."""
        left = None
        right = None
        for interval in intervals:
            left = min(left, interval[0]) if left is not None else interval[0]
            right = max(right, interval[1]) if right is not None else interval[1]
        left = left if left is not None else 0.0
        right = right if right is not None else 1.0
        extra = (right - left) * 0.5
        self.left_channel = int(max(0.0, left - extra) * data_and_metadata.data_shape[-1])
        self.right_channel = int(min(1.0, right + extra) * data_and_metadata.data_shape[-1])
        data_min = numpy.amin(data_and_metadata.data[..., self.left_channel:self.right_channel])
        data_max = numpy.amax(data_and_metadata.data[..., self.left_channel:self.right_channel])
        if data_min > 0 and data_max > 0:
            self.y_min = 0.0
            self.y_max = data_max * 1.2
        elif data_min < 0 and data_max < 0:
            self.y_min = data_min * 1.2
            self.y_max = 0.0
        else:
            self.y_min = data_min * 1.2
            self.y_max = data_max * 1.2

    def view_to_selected_graphics(self, data_and_metadata: DataAndMetadata.DataAndMetadata) -> None:
        """Change the view to encompass the selected graphic intervals."""
        all_graphics = self.graphics
        graphics = [graphic for graphic_index, graphic in enumerate(all_graphics) if self.graphic_selection.contains(graphic_index)]
        intervals = list()
        for graphic in graphics:
            if isinstance(graphic, Graphics.IntervalGraphic):
                intervals.append(graphic.interval)
        self.view_to_intervals(data_and_metadata, intervals)

    @property
    def preview_2d_shape(self) -> typing.Optional[typing.Tuple[int, ...]]:
        if not self.__data_and_metadata:
            return None
        data_and_metadata = self.__data_and_metadata
        dimensional_shape = data_and_metadata.dimensional_shape
        next_dimension = 0
        if data_and_metadata.is_sequence:
            next_dimension += 1
        if data_and_metadata.is_collection:
            collection_dimension_count = data_and_metadata.collection_dimension_count
            datum_dimension_count = data_and_metadata.datum_dimension_count
            # next dimensions are treated as collection indexes.
            if collection_dimension_count == 1 and datum_dimension_count == 1:
                return dimensional_shape[next_dimension:next_dimension + collection_dimension_count + datum_dimension_count]
            elif collection_dimension_count == 2 and datum_dimension_count == 1:
                return dimensional_shape[next_dimension:next_dimension + collection_dimension_count]
            else:  # default, "pick"
                return dimensional_shape[next_dimension + collection_dimension_count:next_dimension + collection_dimension_count + datum_dimension_count]
        else:
            return dimensional_shape[next_dimension:]

    @property
    def selected_graphics(self):
        return [self.graphics[i] for i in self.graphic_selection.indexes]

    def __validate_display_limits(self, value):
        if value is not None:
            if len(value) == 0:
                return None
            elif len(value) == 1:
                return (value[0], None) if value[0] is not None else None
            elif value[0] is not None and value[1] is not None:
                return min(value[0], value[1]), max(value[0], value[1])
            elif value[0] is None and value[1] is None:
                return None
            else:
                return value[0], value[1]
        return value

    def __validate_sequence_index(self, value: int) -> int:
        if not self._is_reading:
            if self.__data_and_metadata and self.__data_and_metadata.dimensional_shape is not None:
                return max(min(int(value), self.__data_and_metadata.max_sequence_index - 1), 0) if self.__data_and_metadata.is_sequence else 0
        return 0

    def __validate_collection_index(self, value: typing.Tuple[int, int, int]) -> typing.Tuple[int, int, int]:
        if not self._is_reading:
            if self.__data_and_metadata and self.__data_and_metadata.dimensional_shape is not None:
                dimensional_shape = self.__data_and_metadata.dimensional_shape
                collection_base_index = 1 if self.__data_and_metadata.is_sequence else 0
                collection_dimension_count = self.__data_and_metadata.collection_dimension_count
                i0 = max(min(int(value[0]), dimensional_shape[collection_base_index + 0] - 1), 0) if collection_dimension_count > 0 else 0
                i1 = max(min(int(value[1]), dimensional_shape[collection_base_index + 1] - 1), 0) if collection_dimension_count > 1 else 0
                i2 = max(min(int(value[2]), dimensional_shape[collection_base_index + 2] - 1), 0) if collection_dimension_count > 2 else 0
                return i0, i1, i2
        return (0, 0, 0)

    def __validate_slice_center_for_width(self, value, slice_width):
        if self.__data_and_metadata and self.__data_and_metadata.dimensional_shape is not None:
            depth = self.__data_and_metadata.dimensional_shape[-1]
            mn = max(int(slice_width * 0.5), 0)
            mx = min(int(depth - slice_width * 0.5), depth - 1)
            return min(max(int(value), mn), mx)
        return value if self._is_reading else 0

    def __validate_slice_center(self, value):
        return self.__validate_slice_center_for_width(value, self.slice_width)

    def __validate_slice_width(self, value):
        if self.__data_and_metadata and self.__data_and_metadata.dimensional_shape is not None:
            depth = self.__data_and_metadata.dimensional_shape[-1]  # signal_index
            slice_center = self.slice_center
            mn = 1
            mx = max(min(slice_center, depth - slice_center) * 2, 1)
            return min(max(value, mn), mx)
        return value if self._is_reading else 1

    def validate_slice_indexes(self) -> None:
        sequence_index = self.__validate_sequence_index(self.sequence_index)
        if sequence_index != self.sequence_index:
            self.sequence_index = sequence_index

        collection_index = self.__validate_collection_index(self.collection_index)
        if collection_index != self.collection_index:
            self.collection_index = collection_index

        slice_center = self.__validate_slice_center_for_width(self.slice_center, 1)
        if slice_center != self.slice_center:
            old_slice_width = self.slice_width
            self.slice_width = 1
            self.slice_center = self.slice_center
            self.slice_width = old_slice_width

    @property
    def actual_display_type(self):
        display_type = self.display_type
        data_and_metadata = self.__data_and_metadata
        valid_data = functools.reduce(operator.mul, self.preview_2d_shape) > 0 if self.preview_2d_shape is not None else False
        if valid_data and data_and_metadata and not display_type in ("line_plot", "image"):
            if data_and_metadata.collection_dimension_count == 2 and data_and_metadata.datum_dimension_count == 1:
                display_type = "image"
            elif data_and_metadata.datum_dimension_count == 1:
                display_type = "line_plot"
            elif data_and_metadata.datum_dimension_count == 2:
                display_type = "image"
        return display_type

    def get_line_plot_display_parameters(self, display_values):
        data_and_metadata = self.data_and_metadata_for_display_panel
        if data_and_metadata:

            class LinePlotDisplayParameters:
                def __init__(self, display, calculated_display_values):
                    displayed_dimensional_calibrations = display.displayed_dimensional_calibrations
                    metadata = data_and_metadata.metadata
                    dimensional_shape = data_and_metadata.dimensional_shape
                    displayed_dimensional_calibration = displayed_dimensional_calibrations[-1] if len(displayed_dimensional_calibrations) > 0 else Calibration.Calibration()
                    displayed_intensity_calibration = copy.deepcopy(data_and_metadata.intensity_calibration)
                    display_data_and_metadata = calculated_display_values.display_data_and_metadata
                    display_data = display_data_and_metadata.data if display_data_and_metadata else None
                    self.display_data = display_data
                    self.dimensional_shape = dimensional_shape
                    self.displayed_intensity_calibration = displayed_intensity_calibration
                    self.displayed_dimensional_calibration = displayed_dimensional_calibration
                    self.metadata = metadata
                    self.y_range = display.y_min, display.y_max
                    self.y_style = display.y_style
                    self.channel_range = display.left_channel, display.right_channel
                    self.legend_labels = display.legend_labels

            return LinePlotDisplayParameters(self, display_values)
        return None

    def get_image_display_parameters(self, display_values):
        data_and_metadata = self.data_and_metadata_for_display_panel
        if data_and_metadata:

            class ImageDisplayParameters:
                def __init__(self, display, calculated_display_values):
                    displayed_dimensional_calibrations = display.displayed_dimensional_calibrations
                    if len(displayed_dimensional_calibrations) == 0:
                        dimensional_calibration = Calibration.Calibration()
                    elif len(displayed_dimensional_calibrations) == 1:
                        dimensional_calibration = displayed_dimensional_calibrations[0]
                    else:
                        display_data_and_metadata = calculated_display_values.display_data_and_metadata
                        if display_data_and_metadata:
                            dimensional_calibration = display_data_and_metadata.dimensional_calibrations[-1]
                        else:
                            dimensional_calibration = Calibration.Calibration()
                    self.display_rgba = calculated_display_values.display_rgba
                    self.display_rgba_shape = display.preview_2d_shape
                    self.display_rgba_timestamp = calculated_display_values.display_rgba_timestamp
                    self.dimensional_calibration = dimensional_calibration
                    self.metadata = data_and_metadata.metadata

            return ImageDisplayParameters(self, display_values)
        return None

    @property
    def slice_interval(self):
        if self.__data_and_metadata and self.__data_and_metadata.dimensional_shape is not None:
            depth = self.__data_and_metadata.dimensional_shape[-1]  # signal_index
            if depth > 0:
                slice_interval_start = int(self.slice_center + 1 - self.slice_width * 0.5)
                slice_interval_end = slice_interval_start + self.slice_width
                return (float(slice_interval_start) / depth, float(slice_interval_end) / depth)
            return 0, 0
        return None

    @slice_interval.setter
    def slice_interval(self, slice_interval):
        if self.__data_and_metadata.dimensional_shape is not None:
            depth = self.__data_and_metadata.dimensional_shape[-1]  # signal_index
            if depth > 0:
                slice_interval_center = int(((slice_interval[0] + slice_interval[1]) * 0.5) * depth)
                slice_interval_width = int((slice_interval[1] - slice_interval[0]) * depth)
                self.slice_center = slice_interval_center
                self.slice_width = slice_interval_width

    def __slice_interval_changed(self, name, value):
        # notify for dependent slice_interval property
        self.__property_changed(name, value)
        self.notify_property_changed("slice_interval")

    def __display_type_changed(self, property_name, value):
        self.__property_changed(property_name, value)
        self.display_type_changed_event.fire()

    def __color_map_id_changed(self, property_name, value):
        self.__property_changed(property_name, value)
        if value:
            lookup_table_options = ColorMaps.color_maps
            self.__color_map_data = lookup_table_options.get(value)
        else:
            self.__color_map_data = None
        self.__property_changed("color_map_data", self.__color_map_data)

    @property
    def color_map_data(self) -> typing.Optional[numpy.ndarray]:
        """Return the color map data as a uint8 ndarray with shape (256, 3)."""
        if self.preview_2d_shape is None:  # is there display data?
            return None
        else:
            return self.__color_map_data if self.__color_map_data is not None else ColorMaps.color_maps.get("grayscale")

    def __property_changed(self, property_name, value):
        # when one of the defined properties changes, this gets called
        self.notify_property_changed(property_name)
        self.display_changed_event.fire()
        if property_name in ("sequence_index", "collection_index", "slice_center", "slice_width", "complex_display_type", "display_limits", "color_map_data"):
            self.display_data_will_change_event.fire()
            getattr(self.__calculated_display_values, "_set_" + property_name)(value)
            self.__send_next_calculated_display_values()
        if property_name in ("dimensional_calibration_style", ):
            self.notify_property_changed("displayed_dimensional_calibrations")
            self.notify_property_changed("displayed_intensity_calibration")
            self._get_persistent_property("display_calibrated_values").value = (value in ("calibrated", "calibrated-center"))

    # message sent from buffered_data_source when data changes.
    # thread safe
    def update_data(self, data_and_metadata):
        old_data_shape = self.__data_and_metadata.data_shape if self.__data_and_metadata else None
        self.__data_and_metadata = data_and_metadata
        new_data_shape = self.__data_and_metadata.data_shape if self.__data_and_metadata else None
        if old_data_shape != new_data_shape:
            self.validate_slice_indexes()
        self.__calculated_display_values._set_data_and_metadata(data_and_metadata)
        self.__send_next_calculated_display_values()
        self.notify_property_changed("displayed_dimensional_calibrations")
        self.notify_property_changed("displayed_intensity_calibration")
        self.display_changed_event.fire()

    def set_storage_cache(self, storage_cache):
        self.__cache.set_storage_cache(storage_cache, self)

    def add_calculated_display_values_listener(self, callback, send=True):
        listener = self.__calculated_display_values_available_event.listen(callback)
        if send:
            self.__send_next_calculated_display_values()
        return listener

    def __calculate_display_values(self) -> None:
        """Calculate the display values and send out the calculated values to listeners.

        This method should be called from a thread.

        If the underlying data changes while this method is computing, the values pending flag
        will be set; and if that occurs, this method will be relaunched in another thread.

        The thread-after-thread is used instead of a while loop as a simple way to tell when the
        current display values have been calculated (join on the thread).

        When shutting down this class, however, we need to ensure that another thread is not
        launched; to do that there is a values_thread_ok flag which can be cleared to prevent
        additional threads from being launched.
        """
        try:
            if self._calculated_display_values_test_exception:  # for testing
                raise Exception()
            # calculate the display values
            next_calculated_display_values = self.__calculated_display_values.copy_and_calculate()
            display_values = next_calculated_display_values.values()
            # send them to listeners
            self.__calculated_display_values_available_event.fire(display_values)
        except Exception as e:
            if not self._calculated_display_values_test_exception:
                import traceback
                traceback.print_exc()
                traceback.print_stack()
            self._calculated_display_values_test_exception = False  # for testing
        with self.__calculated_display_values_lock:
            was_pending = self.__calculated_display_values_pending
            self.__calculated_display_values_pending = False
            if was_pending and self.__calculated_display_values_thread_ok:
                with self.__calculated_display_values_thread_lock:
                    calculated_display_values_thread = threading.Thread(target=self.__calculate_display_values)
                    calculated_display_values_thread.start()
                    self.__calculated_display_values_thread = calculated_display_values_thread
            else:
                self.__calculated_display_values_thread = None

    def __send_next_calculated_display_values(self) -> None:
        """Start thread to send next display values, if necessary.

        If the thread is already running, set the display values pending flag.

        Otherwise, start the thread to calculate the display values.
        """
        with self.__calculated_display_values_lock:
            if self.__calculated_display_values_thread:
                self.__calculated_display_values_pending = True
            else:
                self.__calculated_display_values_pending = False
                if self.__calculated_display_values_available_event.listener_count > 0:
                    with self.__calculated_display_values_thread_lock:
                        calculated_display_values_thread = threading.Thread(target=self.__calculate_display_values)
                        calculated_display_values_thread.start()
                        self.__calculated_display_values_thread = calculated_display_values_thread

    def _send_display_values_for_test(self):
        self.__send_next_calculated_display_values()

    def update_calculated_display_values(self) -> None:
        """Update the display values and store the latest version.

        If a calculation thread is running, wait for it to end, at which point the values will be stored and the
        changed message will be sent.

        Otherwise, immediately calculate and store the values and send out the changed message.
        """
        with self.__calculated_display_values_thread_lock:
            calculated_display_values_thread = self.__calculated_display_values_thread
        if calculated_display_values_thread:
            calculated_display_values_thread.join()
        else:
            next_calculated_display_values = self.__calculated_display_values.copy_and_calculate()
            with self.__calculated_display_values_lock:
                self.__calculated_display_values_pending = False
            self.__calculated_display_values_available_event.fire(next_calculated_display_values.values())

    def get_calculated_display_values(self, immediate: bool = False) -> DisplayValues:
        """Return the display values, optionally forcing calculation."""
        if immediate:
            self.update_calculated_display_values()
        return self.__calculated_display_values.values()

    def __insert_graphic(self, name, before_index, graphic):
        graphic.about_to_be_inserted(self)
        graphic_changed_listener = graphic.graphic_changed_event.listen(functools.partial(self.graphic_changed, graphic))
        self.__graphic_changed_listeners.insert(before_index, graphic_changed_listener)
        self.__graphics_map[graphic.uuid] = graphic
        self.graphic_selection.insert_index(before_index)
        self.display_changed_event.fire()
        self.notify_insert_item("graphics", graphic, before_index)

    def __remove_graphic(self, name, index, graphic):
        graphic.about_to_be_removed()
        self.__graphics_map.pop(graphic.uuid)
        self.__disconnect_graphic(graphic, index)
        graphic.close()

    def __disconnect_graphic(self, graphic, index):
        graphic_changed_listener = self.__graphic_changed_listeners[index]
        graphic_changed_listener.close()
        self.__graphic_changed_listeners.remove(graphic_changed_listener)
        self.graphic_selection.remove_index(index)
        self.display_changed_event.fire()
        self.notify_remove_item("graphics", graphic, index)

    def insert_graphic(self, before_index, graphic):
        """Insert a graphic before the index, but do it through the container, so dependencies can be tracked."""
        self.insert_model_item(self, "graphics", before_index, graphic)

    def add_graphic(self, graphic):
        """Append a graphic, but do it through the container, so dependencies can be tracked."""
        self.insert_model_item(self, "graphics", self.item_count("graphics"), graphic)

    def remove_graphic(self, graphic):
        """Remove a graphic, but do it through the container, so dependencies can be tracked."""
        self.remove_model_item(self, "graphics", graphic)

    def get_graphic_by_uuid(self, graphic_uuid: uuid.UUID) -> Graphics.Graphic:
        return self.__graphics_map.get(graphic_uuid)

    # this message comes from the graphic. the connection is established when a graphic
    # is added or removed from this object.
    def graphic_changed(self, graphic):
        self.display_changed_event.fire()

    @property
    def displayed_dimensional_calibrations(self) -> typing.Sequence[Calibration.Calibration]:
        dimensional_calibration_style = self.dimensional_calibration_style
        if (dimensional_calibration_style is None or dimensional_calibration_style == "calibrated") and self.__data_and_metadata:
            return self.__data_and_metadata.dimensional_calibrations
        elif dimensional_calibration_style == "calibrated-center" and self.__data_and_metadata:
            dimensional_calibrations = copy.deepcopy(self.__data_and_metadata.dimensional_calibrations)
            dimensional_shape = self.__data_and_metadata.dimensional_shape
            for dimensional_calibration, dimension in zip(dimensional_calibrations, dimensional_shape):
                dimensional_calibration.offset -= dimension // 2 * dimensional_calibration.scale
            return dimensional_calibrations
        else:
            dimensional_shape = self.__data_and_metadata.dimensional_shape if self.__data_and_metadata is not None else None
            if dimensional_shape is not None:
                if dimensional_calibration_style == "relative-top-left":
                    return [Calibration.Calibration(scale=1.0/display_dimension) for display_dimension in dimensional_shape]
                elif dimensional_calibration_style == "relative-center":
                    return [Calibration.Calibration(scale=2.0/display_dimension, offset=-1.0) for display_dimension in dimensional_shape]
                elif dimensional_calibration_style == "pixels-top-left":
                    return [Calibration.Calibration() for display_dimension in dimensional_shape]
                else:  # "pixels-center"
                    return [Calibration.Calibration(offset=-display_dimension/2) for display_dimension in dimensional_shape]
            else:
                return list()

    @property
    def displayed_intensity_calibration(self):
        if (self.dimensional_calibration_style in ("calibrated", "calibrated-center")) and self.__data_and_metadata:
            return self.__data_and_metadata.intensity_calibration
        else:
            return Calibration.Calibration()

    def __get_calibrated_value_text(self, value: float, intensity_calibration) -> str:
        if value is not None:
            return intensity_calibration.convert_to_calibrated_value_str(value)
        elif value is None:
            return _("N/A")
        else:
            return str(value)

    def get_value_and_position_text(self, pos) -> (str, str):
        data_and_metadata = self.__data_and_metadata
        dimensional_calibrations = self.displayed_dimensional_calibrations
        intensity_calibration = self.displayed_intensity_calibration

        if data_and_metadata is None or pos is None:
            return str(), str()

        is_sequence = data_and_metadata.is_sequence
        collection_dimension_count = data_and_metadata.collection_dimension_count
        datum_dimension_count = data_and_metadata.datum_dimension_count
        if is_sequence:
            pos = (self.sequence_index, ) + pos
        if collection_dimension_count == 2 and datum_dimension_count == 1:
            pos = pos + (self.slice_center, )
        else:
            pos = tuple(self.collection_index[0:collection_dimension_count]) + pos

        while len(pos) < data_and_metadata.datum_dimension_count:
            pos = (0,) + tuple(pos)

        assert len(pos) == len(data_and_metadata.dimensional_shape)

        position_text = ""
        value_text = ""
        data_shape = data_and_metadata.data_shape
        if len(pos) == 4:
            # 4d image
            # make sure the position is within the bounds of the image
            if 0 <= pos[0] < data_shape[0] and 0 <= pos[1] < data_shape[1] and 0 <= pos[2] < data_shape[2] and 0 <= pos[3] < data_shape[3]:
                position_text = u"{0}, {1}, {2}, {3}".format(
                    dimensional_calibrations[3].convert_to_calibrated_value_str(pos[3], value_range=(0, data_shape[3]), samples=data_shape[3]),
                    dimensional_calibrations[2].convert_to_calibrated_value_str(pos[2], value_range=(0, data_shape[2]), samples=data_shape[2]),
                    dimensional_calibrations[1].convert_to_calibrated_value_str(pos[1], value_range=(0, data_shape[1]), samples=data_shape[1]),
                    dimensional_calibrations[0].convert_to_calibrated_value_str(pos[0], value_range=(0, data_shape[0]), samples=data_shape[0]))
                value_text = self.__get_calibrated_value_text(data_and_metadata.get_data_value(pos), intensity_calibration)
        if len(pos) == 3:
            # 3d image
            # make sure the position is within the bounds of the image
            if 0 <= pos[0] < data_shape[0] and 0 <= pos[1] < data_shape[1] and 0 <= pos[2] < data_shape[2]:
                position_text = u"{0}, {1}, {2}".format(dimensional_calibrations[2].convert_to_calibrated_value_str(pos[2], value_range=(0, data_shape[2]), samples=data_shape[2]),
                    dimensional_calibrations[1].convert_to_calibrated_value_str(pos[1], value_range=(0, data_shape[1]), samples=data_shape[1]),
                    dimensional_calibrations[0].convert_to_calibrated_value_str(pos[0], value_range=(0, data_shape[0]), samples=data_shape[0]))
                value_text = self.__get_calibrated_value_text(data_and_metadata.get_data_value(pos), intensity_calibration)
        if len(pos) == 2:
            # 2d image
            # make sure the position is within the bounds of the image
            if len(data_shape) == 1:
                if pos[-1] >= 0 and pos[-1] < data_shape[-1]:
                    position_text = u"{0}".format(dimensional_calibrations[-1].convert_to_calibrated_value_str(pos[-1], value_range=(0, data_shape[-1]), samples=data_shape[-1]))
                    full_pos = [0, ] * len(data_shape)
                    full_pos[-1] = pos[-1]
                    value_text = self.__get_calibrated_value_text(data_and_metadata.get_data_value(full_pos), intensity_calibration)
            else:
                if pos[0] >= 0 and pos[0] < data_shape[0] and pos[1] >= 0 and pos[1] < data_shape[1]:
                    is_polar = dimensional_calibrations[0].units.startswith("1/") and dimensional_calibrations[0].units == dimensional_calibrations[1].units
                    is_polar = is_polar and abs(dimensional_calibrations[0].scale * data_shape[0] - dimensional_calibrations[1].scale * data_shape[1]) < 1e-12
                    is_polar = is_polar and abs(dimensional_calibrations[0].offset / (dimensional_calibrations[0].scale * data_shape[0]) + 0.5) < 1e-12
                    is_polar = is_polar and abs(dimensional_calibrations[1].offset / (dimensional_calibrations[1].scale * data_shape[1]) + 0.5) < 1e-12
                    if is_polar:
                        x = dimensional_calibrations[1].convert_to_calibrated_value(pos[1])
                        y = dimensional_calibrations[0].convert_to_calibrated_value(pos[0])
                        r = math.sqrt(x * x + y * y)
                        angle = -math.atan2(y, x)
                        r_str = dimensional_calibrations[0].convert_to_calibrated_value_str(dimensional_calibrations[0].convert_from_calibrated_value(r), value_range=(0, data_shape[0]), samples=data_shape[0], display_inverted=True)
                        position_text = u"{0}, {1:.4f}° ({2})".format(r_str, math.degrees(angle), _("polar"))
                    else:
                        position_text = u"{0}, {1}".format(dimensional_calibrations[1].convert_to_calibrated_value_str(pos[1], value_range=(0, data_shape[1]), samples=data_shape[1], display_inverted=True),
                            dimensional_calibrations[0].convert_to_calibrated_value_str(pos[0], value_range=(0, data_shape[0]), samples=data_shape[0], display_inverted=True))
                    value_text = self.__get_calibrated_value_text(data_and_metadata.get_data_value(pos), intensity_calibration)
        if len(pos) == 1:
            # 1d plot
            # make sure the position is within the bounds of the line plot
            if pos[0] >= 0 and pos[0] < data_shape[-1]:
                position_text = u"{0}".format(dimensional_calibrations[-1].convert_to_calibrated_value_str(pos[0], value_range=(0, data_shape[-1]), samples=data_shape[-1]))
                full_pos = [0, ] * len(data_shape)
                full_pos[-1] = pos[0]
                value_text = self.__get_calibrated_value_text(data_and_metadata.get_data_value(full_pos), intensity_calibration)
        return position_text, value_text


def display_factory(lookup_id):
    return Display()

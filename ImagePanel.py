# standard libraries
import gettext
import logging
import numbers
import random
import threading
import uuid
import weakref

# third party libraries
import numpy

# local libraries
from nion.swift import DataGroup
from nion.swift import DataItem
from nion.swift.Decorators import ProcessingThread
from nion.swift.Decorators import queue_main_thread
from nion.swift import Graphics
from nion.swift import Image
from nion.swift import Inspector
from nion.swift import Panel

_ = gettext.gettext


# coordinate systems:
#   widget (origin top left, size of the widget)
#   image_norm ((0,0), (1,1))
#   image_pixel (0,0 size of the image in pixels)
#   calibrated


# how sizing works:
#   the canvas is initially set to fit to the space, meaning all of it is visible
#   when the user presses the fit, fill, or 1:1 buttons, the canvas is resized to match that choice
#   when the window is resized, a best attempt is made to keep the view roughly the same. this may
#     be impossible when the shape of the view changes radically.
#   when the user zooms in/out, the canvas is made larger or smaller by the appropriate amount.

# how to make sure it works:
#   if the new view default is 'fill' or '1:1', do the scroll bars come up in the center?
#   for new view, does zoom go into the center point?
#   switch to 'fit', does zoom still go into center point?


# refer to Illustrator / Default keyboard shortcuts
# http://help.adobe.com/en_US/illustrator/cs/using/WS714a382cdf7d304e7e07d0100196cbc5f-6426a.html

# KEYS FOR CHOOSING TOOLS               ACTION/KEY
# selection tool (whole object)         v
# direct selection tool (parts)         a
# line tool                             \
# rectangle tool                        m
# ellipse tool                          l
# rotate tool                           r
# scale tool                            s
# hand tool (moving image)              h
# zoom tool (zooming image)             z

# KEYS FOR VIEWING IMAGES               ACTION/KEY
# fit image to area                     double w/ hand tool
# magnify to 100%                       double w/ zoom tool
# fit image to area                     0
# fill image to area                    Shift-0
# make image 1:1                        1
# display original image                o

# KEYS FOR DRAWING GRAPHICS             ACTION/KEY
# constrain shape                       shift-drag
# move while draging                    spacebar-drag
# drag from center                      alt-drag (Windows), option-drag (Mac OS)

# KEYS FOR SELECTING GRAPHICS           ACTION/KEY
# use last used selection tool          ctrl (Windows), command (Mac OS)
# add/subtract from selection           alt (Windows), option (Mac OS)

# KEYS FOR MOVING SELECTION/IMAGE       ACTION/KEY
# move in small increments              arrow keys
# move in 10x increments                shift- arrow keys

# KEYS FOR USING PANELS                 ACTION/KEY
# hide all panels                       tab
# hide all panels except data panel     shift-tab

# FUNCTION KEYS                         ACTION/KEY
# tbd


class WidgetMapping(object):
    def __init__(self, image_panel):
        self.image_panel = image_panel
    def map_point_image_norm_to_widget(self, p):
        return self.image_panel.map_image_norm_to_widget(p)
    def map_size_image_norm_to_widget(self, s):
        ms = self.map_point_image_norm_to_widget(s)
        ms0 = self.map_point_image_norm_to_widget((0,0))
        return (ms[0] - ms0[0], ms[1] - ms0[1])
    def map_size_image_to_image_norm(self, s):
        ms = self.map_point_image_to_image_norm(s)
        ms0 = self.map_point_image_to_image_norm((0,0))
        return (ms[0] - ms0[0], ms[1] - ms0[1])
    def map_point_widget_to_image_norm(self, p):
        return self.image_panel.map_widget_to_image_norm(p)
    def map_point_widget_to_image(self, p):
        return self.image_panel.map_widget_to_image(p)
    def map_point_image_norm_to_image(self, p):
        return self.image_panel.map_image_norm_to_image(p)
    def map_point_image_to_image_norm(self, p):
        return self.image_panel.map_image_to_image_norm(p)

class GraphicSelection(object):
    def __init__(self):
        self.__weak_listeners = []
        self.__indexes = set()
    # implement listener architecture
    def _notify_listeners(self):
        for weak_listener in self.__weak_listeners:
            listener = weak_listener()
            listener.selection_changed(self)
    def add_listener(self, listener):
        self.__weak_listeners.append(weakref.ref(listener))
    def remove_listener(self, listener):
        self.__weak_listeners.remove(weakref.ref(listener))
    # manage selection
    def __get_current_index(self):
        if len(self.__indexes) == 1:
            for index in self.__indexes:
                return index
        return None
    current_index = property(__get_current_index)
    def has_selection(self):
        return len(self.__indexes) > 0
    def contains(self, index):
        return index in self.__indexes
    def __get_indexes(self):
        return self.__indexes
    indexes = property(__get_indexes)
    def clear(self):
        old_index = self.__indexes.copy()
        self.__indexes = set()
        if old_index != self.__indexes:
            self._notify_listeners()
    def add(self, index):
        assert isinstance(index, numbers.Integral)
        old_index = self.__indexes.copy()
        self.__indexes.add(index)
        if old_index != self.__indexes:
            self._notify_listeners()
    def remove(self, index):
        assert isinstance(index, numbers.Integral)
        old_index = self.__indexes.copy()
        self.__indexes.remove(index)
        if old_index != self.__indexes:
            self._notify_listeners()
    def set(self, index):
        assert isinstance(index, numbers.Integral)
        old_index = self.__indexes.copy()
        self.__indexes = set()
        self.__indexes.add(index)
        if old_index != self.__indexes:
            self._notify_listeners()
    def toggle(self, index):
        assert isinstance(index, numbers.Integral)
        old_index = self.__indexes.copy()
        if index in self.__indexes:
            self._indexes.remove(index)
        else:
            self._indexes.add(index)
        if old_index != self.__indexes:
            self._notify_listeners()


class DisplayThread(ProcessingThread):

    def __init__(self, image_panel):
        super(DisplayThread, self).__init__()
        self.__image_panel = image_panel
        self.__data_item = None
        # don't start until everything is initialized
        self.start()

    def handle_data(self, data_item):
        if self.__data_item:
            self.__data_item.remove_ref()
        self.__data_item = data_item
        if data_item:
            data_item.add_ref()

    def grab_data(self):
        data_item = self.__data_item
        self.__data_item = None
        return data_item

    def process_data(self, data_item):
        assert data_item is not None
        self.__image_panel._repaint(data_item)

    def release_data(self, data_item):
        assert data_item is not None
        data_item.remove_ref()


class ImagePanel(Panel.Panel):

    def __init__(self, document_controller, panel_id, properties):
        super(ImagePanel, self).__init__(document_controller, panel_id, _("Image Panel"))

        self.graphic_drag_items = []
        self.graphic_drag_item = None
        self.graphic_part_data = {}
        self.graphic_drag_indexes = []

        self.graphic_selection = GraphicSelection()
        self.graphic_selection.add_listener(self)

        self.last_mouse = None

        self.zoom = 1.0
        self.tx = 0.0
        self.ty = 0.0

        self.__data_panel_selection = DataItem.DataItemSpecifier()

        self.__weak_listeners = []

        self.__mouse_in = False

        self.__block_scrollers = False

        self.image_canvas_zoom = 1.0
        self.image_canvas_center = (0.5, 0.5)
        self.image_canvas_mode = "fit"
        self.image_canvas_preserve_pos = True
        # the first time that this object receives the viewport_changed message,
        # the canvas will be exactly the same size as the viewport. this variable
        # helps to avoid setting the scrollbar positions until after the canvas
        # size is set.
        self.image_canvas_first = True

        self.image_canvas = self.ui.create_canvas_widget()
        self.image_canvas.focusable = True
        self.image_canvas.on_size_changed = lambda width, height: self.size_changed(width, height)
        self.image_canvas.on_focus_changed = lambda focused: self.focus_changed(focused)
        self.image_canvas.on_mouse_entered = lambda: self.mouse_entered()
        self.image_canvas.on_mouse_exited = lambda: self.mouse_exited()
        self.image_canvas.on_mouse_clicked = lambda x, y, modifiers: self.mouse_clicked((y, x), modifiers)
        self.image_canvas.on_mouse_pressed = lambda x, y, modifiers: self.mouse_pressed((y, x), modifiers)
        self.image_canvas.on_mouse_released = lambda x, y, modifiers: self.mouse_released((y, x), modifiers)
        self.image_canvas.on_mouse_position_changed = lambda x, y, modifiers: self.mouse_position_changed((y, x), modifiers)
        self.image_canvas.on_key_pressed = lambda text, key, modifiers: self.key_pressed(text, key, modifiers)

        fit_button = self.ui.create_push_button_widget("Fit")
        fill_button = self.ui.create_push_button_widget("Fill")
        one_to_one_button = self.ui.create_push_button_widget("1:1")
        show_source_button = self.ui.create_push_button_widget("Up")

        fit_button.on_clicked = self.__set_fit_mode
        fill_button.on_clicked = self.__set_fill_mode
        one_to_one_button.on_clicked = self.__set_one_to_one_mode
        show_source_button.on_clicked = self.__show_data_source

        self.image_controls = self.ui.create_row_widget()
        self.image_controls.add(fit_button)
        self.image_controls.add(fill_button)
        self.image_controls.add(one_to_one_button)
        self.image_controls.add_spacing(12)
        self.image_controls.add(show_source_button)
        self.image_controls.add_stretch()

        self.image_canvas_scroll = self.ui.create_scroll_area_widget()
        self.image_canvas_scroll.content = self.image_canvas
        self.image_canvas_scroll.on_viewport_changed = lambda rect: self.update_image_canvas_size()

        self.image_widget = self.ui.create_column_widget()
        self.image_widget.add(self.image_controls)
        self.image_widget.add(self.image_canvas_scroll, fill=True)

        self.line_plot_canvas = self.ui.create_canvas_widget()
        self.line_plot_canvas.focusable = True
        self.line_plot_canvas.on_size_changed = lambda width, height: self.size_changed(width, height)
        self.line_plot_canvas.on_focus_changed = lambda focused: self.focus_changed(focused)
        self.line_plot_canvas.on_mouse_clicked = lambda x, y, modifiers: self.mouse_clicked((y, x), modifiers)
        self.line_plot_canvas.on_key_pressed = lambda text, key, modifiers: self.key_pressed(text, key, modifiers)

        self.image_focus_ring_canvas = self.ui.create_canvas_widget()
        self.line_plot_focus_ring_canvas = self.ui.create_canvas_widget()

        self.widget = self.ui.create_stack_widget()
        self.widget.add(self.image_widget)
        self.widget.add(self.line_plot_canvas)

        self.line_plot_canvas.add_overlay(self.line_plot_focus_ring_canvas)
        self.image_canvas_scroll.add_overlay(self.image_focus_ring_canvas)

        self.__display_layer = self.image_canvas.create_layer()
        self.__graphics_layer = self.image_canvas.create_layer()
        self.__line_plot_layer = self.line_plot_canvas.create_layer()
        self.__line_plot_focus_ring_layer = self.line_plot_focus_ring_canvas.create_layer()
        self.__image_focus_ring_layer = self.image_focus_ring_canvas.create_layer()

        self.document_controller.register_image_panel(self)

        self.__display_thread = DisplayThread(self)

        self.closed = False

    def close(self):
        self.closed = True
        self.__display_thread.close()
        self.__display_thread = None
        self.document_controller.unregister_image_panel(self)
        self.graphic_selection.remove_listener(self)
        self.graphic_selection = None
        self.data_panel_selection = DataItem.DataItemSpecifier()  # required before destructing display thread
        super(ImagePanel, self).close()

    # return a dictionary that can be used to restore the content of this image panel
    def save_content(self):
        content = {}
        data_panel_selection = self.data_panel_selection
        if data_panel_selection.data_group and data_panel_selection.data_item:
            content["data-group"] = data_panel_selection.data_group.uuid
            content["data-item"] = data_panel_selection.data_item.uuid
        return content

    # restore content from dictionary and document controller
    def restore_content(self, content, document_controller):
        if "data-group" in content and "data-item" in content:
            data_group_uuid = content["data-group"]
            data_item_uuid = content["data-item"]
            data_group = DataGroup.get_data_group_in_container_by_uuid(document_controller.document_model, data_group_uuid)
            if data_group:
                data_item = document_controller.document_model.get_data_item_by_key(data_item_uuid)
                if data_item:
                    self.data_panel_selection = DataItem.DataItemSpecifier(data_group, data_item)

    def update_image_canvas_size(self):
        if self.closed: return  # argh
        if self.__block_scrollers: return  # argh2
        viewport_size = self.image_canvas_scroll.viewport[1]
        if viewport_size[0] == 0 or viewport_size[1] == 0: return
        if self.data_item:
            if self.data_item.is_data_2d:
                if self.image_canvas_mode == "fill":
                    spatial_size = self.data_item.spatial_shape
                    scale_h = float(spatial_size[1]) / viewport_size[1]
                    scale_v = float(spatial_size[0]) / viewport_size[0]
                    if scale_v < scale_h:
                        canvas_size = (viewport_size[0] * self.image_canvas_zoom, viewport_size[0] * spatial_size[1] / spatial_size[0] * self.image_canvas_zoom)
                    else:
                        canvas_size = (viewport_size[1] * spatial_size[0] / spatial_size[1] * self.image_canvas_zoom, viewport_size[1] * self.image_canvas_zoom)
                elif self.image_canvas_mode == "1:1":
                    canvas_size = self.data_item.spatial_shape
                    canvas_size = (canvas_size[0] * self.image_canvas_zoom, canvas_size[1] * self.image_canvas_zoom)
                else:  # fit
                    canvas_size = (viewport_size[0] * self.image_canvas_zoom, viewport_size[1] * self.image_canvas_zoom)
                old_block_scrollers = self.__block_scrollers
                self.__block_scrollers = True
                #logging.debug("before")
                #self.image_canvas_scroll.info()
                self.image_canvas.size = canvas_size
                #logging.debug("after")
                #self.image_canvas_scroll.info()
                if not self.image_canvas_first and self.image_canvas_preserve_pos:
# scroll bar has a range of 0 to canvas_size - viewport_size
# when scroll bar is minimum, viewport ranges from 0 to viewport_size/canvas_zoom
# when scroll bar is maximum, viewport ranges from (canvas_size - viewportsize)/canvas_zoom to canvas_size/canvas_zoom
# when scroll bar has value, viewport ranges from value/canvas_zoom to (value + viewport_size)/canvas_zoom
# and viewport center is (value + value + viewport_size)/2 / canvas_zoom = (value + viewport_size / 2) / canvas_zoom
# which means value = canvas_center * canvas_zoom - viewport_size / 2
# center = viewport_center / (canvas_size * canvas_zoom)
                    viewport_center = self.map_image_norm_to_widget(self.image_canvas_center)
                    h_range = canvas_size[1] - viewport_size[1]
                    v_range = canvas_size[0] - viewport_size[0]
                    h_offset = (viewport_center[1] - viewport_size[1]*0.5) / h_range if h_range else 0.0
                    v_offset = (viewport_center[0] - viewport_size[0]*0.5) / v_range if v_range else 0.0
                    h_offset = min(max(h_offset, 0.0), 1.0)
                    v_offset = min(max(v_offset, 0.0), 1.0)
                    #logging.debug("self.image_canvas_center %s", self.image_canvas_center)
                    #logging.debug("viewport_center %s", viewport_center)
                    #logging.debug("canvas_size %s  self.image_canvas_zoom %s", canvas_size, self.image_canvas_zoom)
                    #logging.debug("h_offset %s  v_offset %s", h_offset, v_offset)
                    self.image_canvas_scroll.scroll_to(h_offset, v_offset)
                    self.image_canvas_preserve_pos = False
                elif not self.image_canvas_first:
                    viewport = self.image_canvas_scroll.viewport
                    viewport_center = (viewport[0][0] + viewport[1][0]*0.5, viewport[0][1] + viewport[1][1]*0.5)
                    self.image_canvas_center = self.map_widget_to_image_norm(viewport_center)
                    #logging.debug("viewport %s", viewport)
                    #logging.debug("viewport_center %s", viewport_center)
                    #logging.debug("SET self.image_canvas_center %s", self.image_canvas_center)
                self.image_canvas_first = False
                self.__block_scrollers = old_block_scrollers
            else:
                self.image_canvas.size = viewport_size
        else:
            self.image_canvas.size = viewport_size

    def set_focused(self, focused):
        if self.closed: return  # argh
        self.image_canvas.focused = focused
        self.line_plot_canvas.focused = focused
        self.display_changed()

    # this will only be called from the drawing thread (via _repaint)
    def __repaint_graphics(self):
        data_item = self.data_item
        graphics = data_item.graphics if data_item else None
        if not self.closed:
            widget_mapping = WidgetMapping(self)
            ctx = self.__graphics_layer.drawing_context
            ctx.clear()
            ctx.save()
            if self.image_size and graphics:
                for graphic_index, graphic in enumerate(graphics):
                    graphic.draw(ctx, widget_mapping, self.graphic_selection.contains(graphic_index))
            ctx.restore()
            if False:  # display scale marker?
                ctx.begin_path()
                origin = widget_mapping.map_point_image_norm_to_widget((0.95, 0.05))
                ctx.move_to(origin[1], origin[0])
                ctx.line_to(origin[1] + 100, origin[0])
                ctx.line_to(origin[1] + 100, origin[0] - 10)
                ctx.line_to(origin[1], origin[0] - 10)
                ctx.close_path()
                ctx.fill_style = "#448"
                ctx.fill()
                ctx.stroke_style="#000"
                ctx.stroke()
                ctx.font = "normal 24px serif"
                ctx.fill_style = "#FFF"
                ctx.fill_text("60nm", origin[1], origin[0] - 12)

    def __repaint_focus_ring(self, canvas, focused, size):
        ctx = canvas.layers[0].drawing_context
        ctx.clear()
        stroke_style = "#CCC"  # TODO: platform dependent
        if focused:
            stroke_style = "#3876D6"  # TODO: platform dependent
        ctx.begin_path()
        ctx.rect(2, 2, size[1] - 4, size[0] - 4)
        ctx.line_join = "miter"
        ctx.stroke_style = stroke_style
        ctx.line_width = 4.0
        ctx.stroke()

    # this will only be called from the drawing thread
    def _repaint(self, data_item):
        if self.closed: return  # argh
        if data_item and data_item.is_data_1d:
            self.__repaint_line_plot(data_item)
            self.__graphics_layer.drawing_context.clear()
            self.__display_layer.drawing_context.clear()
            self.__line_plot_focus_ring_layer.drawing_context.clear()
            self.__image_focus_ring_layer.drawing_context.clear()
            if self.document_controller.selected_image_panel == self:
                self.__repaint_focus_ring(self.line_plot_focus_ring_canvas, self.line_plot_canvas.focused, self.line_plot_focus_ring_canvas.canvas_size)
            if self.ui and self.line_plot_canvas:
                self.line_plot_canvas.draw()
            if self.ui and self.line_plot_focus_ring_canvas:
                self.line_plot_focus_ring_canvas.draw()
        elif data_item and data_item.is_data_2d:
            self.__repaint_image(data_item)
            self.__repaint_graphics()
            self.__line_plot_layer.drawing_context.clear()
            self.__line_plot_focus_ring_layer.drawing_context.clear()
            self.__image_focus_ring_layer.drawing_context.clear()
            if self.document_controller.selected_image_panel == self:
                viewport_size = self.image_canvas_scroll.viewport[1]
                viewport_size = (viewport_size[0]+1, viewport_size[1]+1)  # tweak it, for some unknown reason it's off by one
                self.__repaint_focus_ring(self.image_focus_ring_canvas, self.image_canvas.focused, viewport_size)
            if self.ui and self.image_canvas:
                self.image_canvas.draw()
            if self.ui and self.image_focus_ring_canvas:
                self.image_focus_ring_canvas.draw()

    # this will only be called from the drawing thread (via _repaint)
    def __repaint_line_plot(self, data_item):

        #logging.debug("enter %s %s", self, time.time())

        assert data_item is not None
        assert data_item.is_data_1d

        data = data_item.data
        assert data is not None
        data = Image.scalarFromArray(data_item.data)  # make sure complex becomes scalar
        assert data is not None
        if Image.is_data_rgb(data) or Image.is_data_rgba(data):
            # note 0=b, 1=g, 2=r, 3=a. calculate luminosity.
            data = 0.0722 * data[:,0] + 0.7152 * data[:,1] + 0.2126 * data[:,2]
        assert data is not None

        rect = ((0, 0), (self.line_plot_canvas.height, self.line_plot_canvas.width))
        ctx = self.__line_plot_layer.drawing_context
        ctx.clear()
        ctx.save()

        data_min = numpy.amin(data)
        data_max = numpy.amax(data)
        data_len = data.shape[0]
        golden_ratio = 1.618
        display_rect = Graphics.fit_to_aspect_ratio(rect, golden_ratio)
        display_width = int(display_rect[1][1])
        display_height = int(display_rect[1][0])
        display_origin_x = int(display_rect[0][1])
        display_origin_y = int(display_rect[0][0])
        ctx.begin_path()
        ctx.move_to(display_origin_x, display_origin_y + display_height)
        for i in xrange(0, display_width,3):
            ctx.line_to(display_origin_x + i, display_origin_y + display_height - (display_height * (float(data[int(data_len*float(i)/display_width)]) - data_min) / (data_max - data_min)))
        ctx.line_to(display_origin_x + display_width-1, display_origin_y + display_height)
        ctx.close_path()
        ctx.fill_style = '#AFA'
        ctx.fill()
        ctx.line_width = 2
        ctx.line_cap = 'round'
        ctx.line_join = 'round'
        ctx.stroke_style = '#2A2'
        ctx.stroke()
        ctx.begin_path()
        ctx.move_to(display_rect[0][1], display_rect[0][0])
        ctx.line_to(display_rect[0][1] + display_rect[1][1], display_rect[0][0])
        ctx.line_to(display_rect[0][1] + display_rect[1][1], display_rect[0][0] + display_rect[1][0])
        ctx.line_to(display_rect[0][1], display_rect[0][0] + display_rect[1][0])
        ctx.close_path()
        ctx.line_width = 1
        ctx.stroke_style = '#888'
        ctx.stroke()

        ctx.restore()

        #logging.debug("exit %s %s", self, time.time())

    # this will only be called from the drawing thread (via _repaint)
    def __repaint_image(self, data_item):

        #logging.debug("enter %s %s", self, time.time())

        assert data_item is not None
        assert data_item.is_data_2d

        rect = ((0, 0), (self.image_canvas.height, self.image_canvas.width))
        ctx = self.__display_layer.drawing_context
        ctx.clear()
        ctx.save()

        rgba_image = data_item.preview_2d

        # this method is called on a thread, so we cannot access self.data_item
        display_rect = self.__calculate_transform_image_for_image_size(data_item.spatial_shape)

        if rgba_image is not None and display_rect and display_rect[1][0] > 0 and display_rect[1][1] > 0:
            ctx.draw_image(rgba_image, display_rect[0][1], display_rect[0][0], display_rect[1][1], display_rect[1][0])

        ctx.restore()

        #logging.debug("exit %s %s", self, time.time())

    # message comes from the view
    def size_changed(self, width, height):
        self.display_changed()
    def focus_changed(self, focused):
        self.display_changed()

    # call this when display needs to be redisplayed
    def display_changed(self):
        if self.closed: return  # argh
        if self.data_item and self.__display_thread:
            self.widget.current_index = 1 if self.data_item.is_data_1d else 0
            self.__display_thread.update_data(self.data_item)
        else:
            self.widget.current_index = 0
            ctx = self.__display_layer.drawing_context
            ctx.clear()
            self.__repaint_graphics()
            if self.ui and self.image_canvas:
                self.image_canvas.draw()
            if self.ui and self.line_plot_canvas:
                self.line_plot_canvas.draw()

    def selection_changed(self, graphic_selection):
        self.display_changed()

    def __get_data_item(self):
        return self.__data_panel_selection.data_item
    data_item = property(__get_data_item)

    def __get_data_item_container(self):
        return self.__data_panel_selection.data_item_container
    data_item_container = property(__get_data_item_container)

    def __get_data_panel_selection(self):
        return self.__data_panel_selection
    def __set_data_panel_selection(self, data_panel_selection):
        assert data_panel_selection is not None
        # assert that either data_group is not None or both are None. it is acceptable
        # to not have a data_item, but not acceptable to have a data_item without a container
        assert data_panel_selection.data_group is not None or data_panel_selection.data_item is None
        assert isinstance(data_panel_selection, DataItem.DataItemSpecifier)
        # track data item in this class to report changes
        if self.data_item_container:
            self.data_item_container.remove_listener(self)
            self.data_item_container.remove_ref()
        if self.data_item:
            self.data_item.remove_listener(self)
            self.data_item.remove_ref()
        self.__data_panel_selection = data_panel_selection
        data_item = self.data_item
        data_item_container = self.data_item_container
        if data_item:
            data_item.add_ref()
            data_item.add_listener(self)
        if data_item_container:
            data_item_container.add_ref()
            data_item_container.add_listener(self)
        for weak_listener in self.__weak_listeners:
            listener = weak_listener()
            listener.data_panel_selection_changed_from_image_panel(data_panel_selection)
        self.data_item_changed(self.data_item, {"property": "source"})
        self.update_image_canvas_size()
    data_panel_selection = property(__get_data_panel_selection, __set_data_panel_selection)

    def data_item_removed(self, container, data_item, index):
        # if our item gets deleted, clear the selection
        if container == self.data_item_container and data_item == self.data_item:
            self.data_panel_selection = DataItem.DataItemSpecifier(self.__data_panel_selection.data_group)

    # tell our listeners the we changed.
    def notify_image_panel_data_item_changed(self, info):
        for weak_listener in self.__weak_listeners:
            listener = weak_listener()
            listener.image_panel_data_item_changed(self, info)

    # this will result in data_item_changed being called when the data item changes.
    def add_listener(self, listener):
        self.__weak_listeners.append(weakref.ref(listener))

    def remove_listener(self, listener):
        self.__weak_listeners.remove(weakref.ref(listener))

    # this message comes from the data item associated with this panel.
    # the connection is established in __set_data_item via data_item.add_listener.
    def data_item_changed(self, data_item, info):
        self.notify_image_panel_data_item_changed(info)
        self.update_cursor_info()
        self.display_changed()

    def mouse_clicked(self, p, modifiers):
        # activate this view. this has the side effect of grabbing focus.
        self.document_controller.selected_image_panel = self

    def mouse_pressed(self, p, modifiers):
        # figure out clicked graphic
        self.graphic_drag_items = []
        self.graphic_drag_item = None
        self.graphic_drag_item_was_selected = False
        self.graphic_part_data = {}
        self.graphic_drag_indexes = []
        if self.data_item:
            for graphic_index, graphic in enumerate(self.data_item.graphics):
                start_drag_pos = p
                already_selected = self.graphic_selection.contains(graphic_index)
                multiple_items_selected = len(self.graphic_selection.indexes) > 1
                move_only = not already_selected or multiple_items_selected
                part = graphic.test(WidgetMapping(self), start_drag_pos, move_only)
                if part:
                    # select item and prepare for drag
                    self.graphic_drag_item_was_selected = self.graphic_selection.contains(graphic_index)
                    if not self.graphic_drag_item_was_selected:
                        if modifiers.shift:
                            self.graphic_selection.add(graphic_index)
                        elif not already_selected:
                            self.graphic_selection.set(graphic_index)
                    # keep track of general drag information
                    self.graphic_drag_start_pos = start_drag_pos
                    self.graphic_drag_changed = False
                    # keep track of info for the specific item that was clicked
                    self.graphic_drag_item = self.data_item.graphics[graphic_index]
                    self.graphic_drag_part = part
                    # keep track of drag information for each item in the set
                    self.graphic_drag_indexes = self.graphic_selection.indexes
                    for index in self.graphic_drag_indexes:
                        graphic = self.data_item.graphics[index]
                        self.graphic_drag_items.append(graphic)
                        self.graphic_part_data[index] = graphic.begin_drag()
                    break
        if not self.graphic_drag_items and not modifiers.shift:
            self.graphic_selection.clear()

    def mouse_released(self, p, modifiers):
        for index in self.graphic_drag_indexes:
            graphic = self.data_item.graphics[index]
            graphic.end_drag(self.graphic_part_data[index])
        if self.graphic_drag_items and not self.graphic_drag_changed:
            graphic_index = self.data_item.graphics.index(self.graphic_drag_item)
            # user didn't move graphic
            if not modifiers.shift:
                # user clicked on a single graphic
                assert self.data_item
                self.graphic_selection.set(graphic_index)
            else:
                # user shift clicked. toggle selection
                # if shift is down and item is already selected, toggle selection of item
                if self.graphic_drag_item_was_selected:
                    self.graphic_selection.remove(graphic_index)
                else:
                    self.graphic_selection.add(graphic_index)
        self.graphic_drag_items = []
        self.graphic_drag_item = None
        self.graphic_part_data = {}
        self.graphic_drag_indexes = []

    def mouse_entered(self):
        self.__mouse_in = True

    def mouse_exited(self):
        self.__mouse_in = False
        self.mouse_position_changed((0, 0), 0)

    def mouse_position_changed(self, p, modifiers):
        if self.closed:  # prevent the last message from sneaking through when closing the window
            return
        # x,y already have transform applied
        self.last_mouse = p
        self.update_cursor_info()
        if self.graphic_drag_items:
            for graphic in self.graphic_drag_items:
                index = self.data_item.graphics.index(graphic)
                part_data = (self.graphic_drag_part, ) + self.graphic_part_data[index]
                graphic.adjust_part(WidgetMapping(self), self.graphic_drag_start_pos, p, part_data, modifiers)
                self.graphic_drag_changed = True
                self.display_changed()

    def update_cursor_info(self):
        pos = None
        image_size = self.image_size
        if self.__mouse_in and self.last_mouse:
            if image_size and len(image_size) > 1:
                pos = self.map_widget_to_image(self.last_mouse)
            data_item = self.data_item
            graphics = data_item.graphics if data_item else None
            selected_graphics = [graphics[index] for index in self.graphic_selection.indexes] if graphics else []
            self.document_controller.notify_listeners("cursor_changed", self.data_item, pos, selected_graphics, image_size)

    def __get_image_size(self):
        data_item = self.data_item
        data_shape = data_item.spatial_shape if data_item else (0,0)
        if not data_shape:
            return (0,0)
        for d in data_shape:
            if not d > 0:
                return None
        return data_shape
    image_size = property(__get_image_size)

    def __calculate_transform_image_for_image_size(self, image_size):
        if self.closed: return  # argh
        if image_size:
            rect = ((0, 0), (self.image_canvas.height, self.image_canvas.width))
            image_rect = Graphics.fit_to_size(rect, image_size)
            image_y = image_rect[0][0] + self.ty*self.zoom - 0.5*image_rect[1][0]*(self.zoom - 1)
            image_x = image_rect[0][1] + self.tx*self.zoom - 0.5*image_rect[1][1]*(self.zoom - 1)
            image_rect = ((image_y, image_x), (image_rect[1][0]*self.zoom, image_rect[1][1]*self.zoom))
            return image_rect
        return None
    def __get_transformed_image_rect(self):
        return self.__calculate_transform_image_for_image_size(self.image_size)
    transformed_image_rect = property(__get_transformed_image_rect)

    # map from image coordinates to widget coordinates
    def map_image_to_widget(self, p):
        image_size = self.image_size
        if image_size:
            return self.map_image_norm_to_widget((float(p[0])/image_size[0], float(p[1])/image_size[1]))
        return None

    # map from image normalized coordinates to widget coordinates
    def map_image_norm_to_widget(self, p):
        transformed_image_rect = self.transformed_image_rect
        if transformed_image_rect:
            return (p[0]*transformed_image_rect[1][0] + transformed_image_rect[0][0], p[1]*transformed_image_rect[1][1] + transformed_image_rect[0][1])
        return None

    # map from widget coordinates to image coordinates
    def map_widget_to_image(self, p):
        transformed_image_rect = self.transformed_image_rect
        image_size = self.image_size
        if transformed_image_rect and image_size:
            if transformed_image_rect[1][0] != 0.0:
                image_y = image_size[0] * (p[0] - transformed_image_rect[0][0])/transformed_image_rect[1][0]
            else:
                image_y = 0
            if transformed_image_rect[1][1] != 0.0:
                image_x = image_size[1] * (p[1] - transformed_image_rect[0][1])/transformed_image_rect[1][1]
            else:
                image_x = 0
            return (image_y, image_x) # c-indexing
        return None

    # map from widget coordinates to image normalized coordinates
    def map_widget_to_image_norm(self, p):
        image_size = self.image_size
        if image_size:
            p_image = self.map_widget_to_image(p)
            return (float(p_image[0]) / image_size[0], float(p_image[1]) / image_size[1])
        return None

    # map from image normalized coordinates to image coordinates
    def map_image_norm_to_image(self, p):
        image_size = self.image_size
        if image_size:
            return (p[0] * image_size[0], p[1] * image_size[1])
        return None

    # map from image normalized coordinates to image coordinates
    def map_image_to_image_norm(self, p):
        image_size = self.image_size
        if image_size:
            return (p[0] / image_size[0], p[1] / image_size[1])
        return None

    def __set_fit_mode(self):
        #logging.debug("---------> fit")
        self.image_canvas_mode = "fit"
        self.image_canvas_preserve_pos = True
        self.image_canvas_zoom = 1.0
        self.image_canvas_center = (0.5, 0.5)
        self.update_image_canvas_size()

    def __set_fill_mode(self):
        #logging.debug("---------> fill")
        self.image_canvas_mode = "fill"
        self.image_canvas_preserve_pos = True
        self.image_canvas_zoom = 1.0
        self.image_canvas_center = (0.5, 0.5)
        self.update_image_canvas_size()

    def __set_one_to_one_mode(self):
        #logging.debug("---------> 1:1")
        self.image_canvas_mode = "1:1"
        self.image_canvas_preserve_pos = True
        self.image_canvas_zoom = 1.0
        self.image_canvas_center = (0.5, 0.5)
        self.update_image_canvas_size()

    def __zoom_in(self):
        self.image_canvas_zoom = self.image_canvas_zoom * 1.05
        self.image_canvas_preserve_pos = True
        self.update_image_canvas_size()

    def __zoom_out(self):
        self.image_canvas_zoom = self.image_canvas_zoom / 1.05
        self.image_canvas_preserve_pos = True
        self.update_image_canvas_size()

    def __show_data_source(self):
        data_source = self.data_item.data_source
        if data_source:
            self.data_panel_selection = DataItem.DataItemSpecifier(self.__data_panel_selection.data_group, data_source)

    # ths message comes from the widget
    def key_pressed(self, text, key, modifiers):
        #logging.debug("text=%s key=%s mod=%s", text, hex(key), modifiers)
        if text == "-":
            self.__zoom_out()
        if text == "+":
            self.__zoom_in()
        if text == "1":
            self.__set_one_to_one_mode()
        if text == "0":
            self.__set_fit_mode()
        if text == ")":
            self.__set_fill_mode()
        if text == "o":
            self.__show_data_source()
        return False


class InfoPanel(Panel.Panel):

    delay_queue = property(lambda self: self.document_controller.delay_queue)

    def __init__(self, document_controller, panel_id, properties):
        super(InfoPanel, self).__init__(document_controller, panel_id, _("Info"))

        ui = document_controller.ui

        self.closed = False

        position_label = ui.create_label_widget(_("Position:"))
        self.position_text = ui.create_label_widget()
        value_label = ui.create_label_widget(_("Value:"))
        self.value_text = ui.create_label_widget()
        self.graphic_text = ui.create_label_widget()

        position_row = ui.create_row_widget(properties={"spacing": 6})
        position_row.add(position_label)
        position_row.add(self.position_text)
        position_row.add_stretch()

        value_row = ui.create_row_widget(properties={"spacing": 6})
        value_row.add(value_label)
        value_row.add(self.value_text)
        value_row.add_stretch()

        graphic_row = ui.create_row_widget(properties={"spacing": 6})
        graphic_row.add(self.graphic_text)
        graphic_row.add_stretch()

        properties["spacing"] = 2
        properties["margin"] = 6
        column = ui.create_column_widget(properties)
        column.add(position_row)
        column.add(value_row)
        column.add(graphic_row)
        column.add_stretch()

        self.widget = column

        # connect self as listener. this will result in calls to selected_data_item_changed and cursor_changed
        self.document_controller.add_listener(self)

    def close(self):
        self.closed = True
        # disconnect self as listener
        self.document_controller.remove_listener(self)
        # finish closing
        super(InfoPanel, self).close()

    # this message is received from the document controller.
    # it is established using add_listener
    @queue_main_thread
    def cursor_changed(self, data_item, pos, selected_graphics, image_size):
        if self.closed:  # may close from underneat since this is queued to main thread
            return
        position_text = ""
        value_text = ""
        graphic_text = ""
        if data_item:
            calibrations = data_item.calculated_calibrations
            if pos:
                # make sure the position is within the bounds of the image
                if pos[0] >= 0 and pos[0] < image_size[0] and pos[1] >= 0 and pos[1] < image_size[1]:
                    position_text = '{0},{1}'.format(calibrations[1].convert_to_calibrated_str(pos[1] - 0.5 * image_size[1]),
                                                     calibrations[0].convert_to_calibrated_str(0.5 * image_size[0] - pos[0]))
                    value = data_item.data[pos[0], pos[1]]
                    if isinstance(value, numbers.Integral):
                        value_text = '{0:d}'.format(value)
                    elif isinstance(value, numbers.Real) or isinstance(value, numbers.Complex):
                        value_text = '{0:f}'.format(value)
                    else:
                        value_text = str(value)
            if len(selected_graphics) == 1:
                graphic = selected_graphics[0]
                graphic_text = graphic.calibrated_description(image_size, calibrations)
        self.position_text.text = position_text
        self.value_text.text = value_text
        self.graphic_text.text = graphic_text

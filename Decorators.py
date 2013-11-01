# standard libraries
import functools
import logging
import threading
import time
import os

# third party libraries
# None

# local libraries
# None

def singleton(cls):
    instances = {}

    def getinstance():
        if cls not in instances:
            instances[cls] = cls()
        return instances[cls]

    return getinstance


def timeit(method):

    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()

        print '%r %2.2f sec' % (method.__name__, te - ts)
        #print '%r (%r, %r) %2.2f sec' % (method.__name__, args, kw, te - ts)
        return result

    return timed


require_main_thread = True


# classes which use this decorator on a method are required
# to define a property: delay_queue.
def queue_main_thread(f):
    @functools.wraps(f)
    def new_function(self, *args, **kw):
        if require_main_thread:
            # using wraps we still get useful info about the function we're calling
            # eg the name
            wrapped_f = functools.wraps(f)(lambda args=args, kw=kw: f(self, *args, **kw))
            self.delay_queue.put(wrapped_f)
        else:
            f(self, *args, **kw)
    return new_function


# classes which use this decorator on a method are required
# to define a property: delay_queue.
def queue_main_thread_sync(f):
    @functools.wraps(f)
    def new_function(self, *args, **kw):
        if require_main_thread:
            # using wraps we still get useful info about the function we're calling
            # eg the name
            e = threading.Event()
            def sync_f(f, event):
                try:
                    f()
                finally:
                    event.set()
            wrapped_f = functools.wraps(f)(lambda args=args, kw=kw: f(self, *args, **kw))
            synced_f = functools.partial(sync_f, wrapped_f, e)
            self.delay_queue.put(synced_f)
            # how do we tell if this is the main (presumably UI) thread?
            # the order from threading.enumerate() is not reliable
            if threading.current_thread().getName() != "MainThread":
                if not e.wait(5):
                    logging.debug("TIMEOUT %s", f)
        else:
            f(self, *args, **kw)
    return new_function


def relative_file(parent_path, filename):
    # nb os.path.abspath is os.path.realpath
    dir = os.path.dirname(os.path.abspath(parent_path))
    return os.path.join(dir, filename)


# experimental class to ref count objects. similar to weakref.
# calls about_to_delete when ref count goes to zero.
class countedref(object):
    objects = {}
    def __init__(self, object):
        self.__object = object
        if self.__object:
            if object in countedref.objects:
                countedref.objects[object] += 1
            else:
                countedref.objects[object] = 1
    def __del__(self):
        if self.__object:
            assert self.__object in countedref.objects
            countedref.objects[self.__object] -= 1
            if countedref.objects[self.__object] == 0:
                del countedref.objects[self.__object]
                self.__object.about_to_delete()
    def __call__(self):
        return self.__object
    def __eq__(self, other):
        return self.__object == other()


# calculates the histogram data and the associated javascript to display
class ProcessingThread(object):

    def __init__(self, minimum_interval=None):
        self.__thread_break = False
        self.__thread_ended_event = threading.Event()
        self.__thread_event = threading.Event()
        self.__thread_lock = threading.Lock()
        self.__thread = threading.Thread(target=self.__process)
        self.__thread.daemon = True
        self.__minimum_interval = minimum_interval
        self.__last_time = 0

    def start(self):
        self.__thread.start()

    def close(self):
        with self.__thread_lock:
            self.__thread_break = True
            self.__thread_event.set()
        self.__thread_ended_event.wait()

    def update_data(self, *args, **kwargs):
        with self.__thread_lock:
            self.handle_data(*args, **kwargs)
            self.__thread_event.set()

    def handle_data(self, data_item):
        raise NotImplementedError()

    def grab_data(self):
        raise NotImplementedError()

    def process_data(self, data):
        raise NotImplementedError()

    def release_data(self, data):
        raise NotImplementedError()

    def __process(self):
        while True:
            self.__thread_event.wait()
            with self.__thread_lock:
                self.__thread_event.clear()  # put this inside lock to avoid race condition
                data = self.grab_data()
            if self.__thread_break:
                if data is not None:
                    self.release_data(data)
                break
            while not self.__thread_break:
                elapsed = time.time() - self.__last_time
                if self.__minimum_interval and elapsed < self.__minimum_interval:
                    self.__thread_event.wait(self.__minimum_interval - elapsed)
                else:
                    break
            if self.__thread_break:
                if data is not None:
                    self.release_data(data)
                break
            try:
                self.process_data(data)
            except Exception as e:
                import traceback
                logging.debug("Processing thread exception %s", e)
                traceback.print_exc()
            finally:
                if data is not None:
                    self.release_data(data)
            self.__last_time = time.time()
        self.__thread_ended_event.set()

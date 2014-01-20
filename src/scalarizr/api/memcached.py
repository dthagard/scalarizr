from scalarizr import rpc
from scalarizr.handlers.memcached import MemcachedInitScript
from scalarizr.util import Singleton


class MemcachedAPI(object):

    __metaclass__ = Singleton

    def __init__(self):
        self.service = MemcachedInitScript()

    @rpc.command_method
    def start_service(self):
        self.service.start()

    @rpc.command_method
    def stop_service(self):
        self.service.stop()

    @rpc.command_method
    def reload(self):
        self.service.reload()

    @rpc.command_method
    def restart(self):
        self.service.restart()

    @rpc.command_method
    def get_service_status(self):
        return self.service.status()

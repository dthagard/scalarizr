'''
Created on Sep 20, 2011

@author: Spike
'''
import os
import time
import logging

from scalarizr.bus import bus
from scalarizr.libs.metaconf import Configuration, NoPathError
from scalarizr.util import initdv2, software
from scalarizr.messaging import Messages
from scalarizr.handlers import ServiceCtlHandler, mysql

BEHAVIOUR = SERVICE_NAME = 'mysql_proxy'
CONFIG_FILE_PATH = '/etc/mysql_proxy.conf'
PID_FILE = '/var/run/mysql-proxy.pid'

def get_handlers():
	return (MysqlProxyHandler(),)

class MysqlProxyInitScript(initdv2.ParametrizedInitScript):
	
	
	def __init__(self):
		res = software.whereis('mysql-proxy')
		if not res:
			raise initdv2.InitdError("Mysql-proxy binary not found. Check your installation")
		self.bin_path = res[0]	
	
	
	def status(self):
		if not os.path.exists(PID_FILE):
			return initdv2.Status.NOT_RUNNING
		
		with open(PID_FILE) as f:
			pid = int(f.read())
		
		try:
			os.kill(pid, 0)
		except OSError:
			try:
				os.remove(PID_FILE)
			except OSError:
				pass
			return initdv2.Status.NOT_RUNNING
		else:
			return initdv2.Status.RUNNING
	
	
	def start(self):
		if not self.running:
			pid = os.fork()
			if pid == 0:
				os.setsid()
				pid = os.fork()
				if pid != 0:
					os._exit(0)

				os.chdir('/')
				os.umask(0)
				
				import resource		# Resource usage information.
				maxfd = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
				if (maxfd == resource.RLIM_INFINITY):
					maxfd = 1024
					
				for fd in range(0, maxfd):
					try:
						os.close(fd)
					except OSError:
						pass
				
				os.open('/dev/null', os.O_RDWR)

				os.dup2(0, 1)
				os.dup2(0, 2)	
				
				try:
					os.execl(self.bin_path, 'mysql-proxy', '--defaults-file=' + CONFIG_FILE_PATH)
				except Exception:
					os._exit(255)
	
	
	def stop(self):
		if self.running:
			with open(PID_FILE) as f:
				pid = int(f.read())
				
			os.kill(pid, 15)
			
			# Check pid is dead
			for i in range(5):
				try:
					os.kill(pid, 0)
				except OSError:
					break
				else:
					time.sleep(1)
			else:
				os.kill(pid, 9)
				
			
	def restart(self):
		self.stop()
		self.start()
	
	
	reload = restart


initdv2.explore(BEHAVIOUR, MysqlProxyInitScript)



class MysqlProxyHandler(ServiceCtlHandler):
	
	
	def __init__(self):
		self._logger = logging.getLogger(__name__)
		self.service = initdv2.lookup(BEHAVIOUR)
		bus.on("reload", self.on_reload)
		self.on_reload()
	
		
	def accept(self, message, queue, behaviour=None, platform=None, os=None, dist=None):
		return mysql.BEHAVIOUR in message.behaviour and \
					message.name in (Messages.HOST_UP,
									 Messages.HOST_DOWN,
									 mysql.MysqlMessages.NEW_MASTER_UP)
	
	
	def on_reload(self):
		self._reload_backends()
				
	def _reload_backends(self):
		self._logger.info('Updating mysql-proxy backends list')
		self.config = Configuration('ini')
		if os.path.exists(CONFIG_FILE_PATH):
			self.config.read(CONFIG_FILE_PATH)
			self.config.remove('./mysql-proxy/proxy-backend-addresses')
			self.config.remove('./mysql-proxy/proxy-read-only-backend-addresses')
			
		try:
			self.config.get('./mysql-proxy')
		except NoPathError:
			self.config.add('./mysql-proxy')
		
		queryenv = bus.queryenv_service
		roles = queryenv.list_roles(behaviour=mysql.BEHAVIOUR)
		master = None
		slaves = []
		for role in roles:
			for host in role.hosts:
				ip = host.internal_ip or host.external_ip
				if host.replication_master:
					master = ip
				else:
					slaves.append(ip)
			
		if master:
			self._logger.debug('Adding mysql master %s to  mysql-proxy defaults file', master)
			self.config.add('./mysql-proxy/proxy-backend-addresses', '%s:3306' % master)
		if slaves:
			self._logger.debug('Adding mysql slaves to  mysql-proxy defaults file: %s', ', '.join(slaves))
			for slave in slaves:
				self.config.add('./mysql-proxy/proxy-read-only-backend-addresses', '%s:3306' % slave)
		
		self.config.set('./mysql-proxy/pid-file', PID_FILE, force=True)
		self.config.set('./mysql-proxy/daemon', 'true', force=True)
		
		self._logger.debug('Saving new mysql-proxy defaults file')
		self.config.write(CONFIG_FILE_PATH)
		os.chmod(CONFIG_FILE_PATH, 0660)

		self.service.restart()

	def on_HostUp(self, message):
		if mysql.BEHAVIOUR in message.behaviour:
			self._reload_backends()

	on_HostDown = on_HostUp

	def on_Mysql_NewMasterUp(self, message):
		self._reload_backends()
		
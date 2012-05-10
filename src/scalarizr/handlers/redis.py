'''
Created on Aug 12, 2011

@author: Dmytro Korsakov
'''
from __future__ import with_statement

import os
import time
import shutil
import tarfile
import tempfile
import logging

from scalarizr import config
from scalarizr.bus import bus
from scalarizr.messaging import Messages
from scalarizr.util import system2, wait_until, initdv2, disttool, software
from scalarizr.util.filetool import split, rchown
from scalarizr.services.redis import Redis, RedisCLI
from scalarizr.service import CnfController
from scalarizr.config import BuiltinBehaviours, ScalarizrState
from scalarizr.handlers import ServiceCtlHandler, HandlerError, DbMsrMessages
from scalarizr.storage import Storage, Snapshot, StorageError, Volume, transfer
from scalarizr.util.iptables import IpTables, RuleSpec, P_TCP
from scalarizr.libs.metaconf import Configuration, NoPathError
from scalarizr.handlers import operation, prepare_tags


BEHAVIOUR = SERVICE_NAME = CNF_SECTION = BuiltinBehaviours.REDIS

STORAGE_PATH 				= '/mnt/redisstorage'
STORAGE_VOLUME_CNF 			= 'redis.json'
STORAGE_SNAPSHOT_CNF 		= 'redis-snap.json'

OPT_REPLICATION_MASTER  	= 'replication_master'
OPT_PERSISTENCE_TYPE		= 'persistence_type'
OPT_MASTER_PASSWORD			= "master_password"
OPT_VOLUME_CNF				= 'volume_config'
OPT_SNAPSHOT_CNF			= 'snapshot_config'

REDIS_CNF_PATH				= 'cnf_path'
UBUNTU_CONFIG_PATH			= '/etc/redis/redis.conf'
CENTOS_CONFIG_PATH			= '/etc/redis.conf'

BACKUP_CHUNK_SIZE 			= 200*1024*1024
DEFAULT_PORT	= 6379


def get_handlers():
	return (RedisHandler(), )


class RedisHandler(ServiceCtlHandler):	
	_logger = None
		
	_queryenv = None
	""" @type _queryenv: scalarizr.queryenv.QueryEnvService	"""
	
	_platform = None
	""" @type _platform: scalarizr.platform.Ec2Platform """
	
	_cnf = None
	''' @type _cnf: scalarizr.config.ScalarizrCnf '''
	
	storage_vol = None	
		
	@property
	def is_replication_master(self):
		value = 0
		if self._cnf.rawini.has_section(CNF_SECTION) and self._cnf.rawini.has_option(CNF_SECTION, OPT_REPLICATION_MASTER):
			value = self._cnf.rawini.get(CNF_SECTION, OPT_REPLICATION_MASTER)
			self._logger.debug('Got %s : %s' % (OPT_REPLICATION_MASTER, value))
		return True if int(value) else False
	
					
	@property
	def redis_tags(self):
		return prepare_tags(BEHAVIOUR, db_replication_role=self.is_replication_master)
		

	@property
	def persistence_type(self):
		value = 'snapshotting'
		if self._cnf.rawini.has_section(CNF_SECTION) and self._cnf.rawini.has_option(CNF_SECTION, OPT_PERSISTENCE_TYPE):
			value = self._cnf.rawini.get(CNF_SECTION, OPT_PERSISTENCE_TYPE)
			self._logger.debug('Got %s : %s' % (OPT_PERSISTENCE_TYPE, value))
		return value
			
			
	def accept(self, message, queue, behaviour=None, platform=None, os=None, dist=None):
		return BEHAVIOUR in behaviour and (
					message.name == DbMsrMessages.DBMSR_NEW_MASTER_UP
				or 	message.name == DbMsrMessages.DBMSR_PROMOTE_TO_MASTER
				or 	message.name == DbMsrMessages.DBMSR_CREATE_DATA_BUNDLE
				or 	message.name == DbMsrMessages.DBMSR_CREATE_BACKUP
				or  message.name == Messages.UPDATE_SERVICE_CONFIGURATION
				or  message.name == Messages.BEFORE_HOST_TERMINATE)	


	def get_initialization_phases(self, hir_message):
		if BEHAVIOUR in hir_message.body:

			steps = [self._step_accept_scalr_conf, self._step_create_storage]
			if hir_message.body[BEHAVIOUR]['replication_master'] == '1':
				steps += [self._step_init_master, self._step_create_data_bundle]
			else:
				steps += [self._step_init_slave]
			steps += [self._step_collect_host_up_data]
			
			return {'before_host_up': [{
				'name': self._phase_redis, 
				'steps': steps
			}]}

	
	def __init__(self):
		self._logger = logging.getLogger(__name__)
		ServiceCtlHandler.__init__(self, SERVICE_NAME, initdv2.lookup('redis'), RedisCnfController())
		bus.on("init", self.on_init)
		bus.define_events(
			'before_%s_data_bundle' % BEHAVIOUR,
			
			'%s_data_bundle' % BEHAVIOUR,
			
			# @param host: New master hostname 
			'before_%s_change_master' % BEHAVIOUR,
			
			# @param host: New master hostname 
			'%s_change_master' % BEHAVIOUR,
			
			'before_slave_promote_to_master',
			
			'slave_promote_to_master'
		)	

		self._phase_redis = 'Configure Redis'
		self._phase_data_bundle = self._op_data_bundle = 'Redis data bundle'
		self._phase_backup = self._op_backup = 'Redis backup'
		self._step_copy_database_file = 'Copy database file'
		self._step_upload_to_cloud_storage = 'Upload data to cloud storage'
		self._step_accept_scalr_conf = 'Accept Scalr configuration'
		self._step_patch_conf = 'Patch configuration files'
		self._step_create_storage = 'Create storage'
		self._step_init_master = 'Initialize Master'
		self._step_init_slave = 'Initialize Slave'
		self._step_create_data_bundle = 'Create data bundle'
		self._step_change_replication_master = 'Change replication Master'
		self._step_collect_host_up_data = 'Collect HostUp data'
		
		self.on_reload()		

	
	def on_init(self):		
			
		bus.on("host_init_response", self.on_host_init_response)
		bus.on("before_host_up", self.on_before_host_up)
		bus.on("before_reboot_start", self.on_before_reboot_start)
		bus.on("before_reboot_finish", self.on_before_reboot_finish)
		
		if self._cnf.state == ScalarizrState.BOOTSTRAPPING:
			self._insert_iptables_rules()
		
		if self._cnf.state == ScalarizrState.RUNNING:

			storage_conf = Storage.restore_config(self._volume_config_path)
			storage_conf['tags'] = self.redis_tags
			self.storage_vol = Storage.create(storage_conf)
			if not self.storage_vol.mounted():
				self.storage_vol.mount()
			
			self.redis.service.start()
			

	def on_reload(self):
		self._queryenv = bus.queryenv_service
		self._platform = bus.platform
		self._cnf = bus.cnf
		ini = self._cnf.rawini
		self._role_name = ini.get(config.SECT_GENERAL, config.OPT_ROLE_NAME)
		
		self._storage_path = STORAGE_PATH
		
		self._volume_config_path  = self._cnf.private_path(os.path.join('storage', STORAGE_VOLUME_CNF))
		self._snapshot_config_path = self._cnf.private_path(os.path.join('storage', STORAGE_SNAPSHOT_CNF))
		
		self.redis = Redis(self.is_replication_master, self.persistence_type)
		
		
	def on_host_init_response(self, message):
		"""
		Check redis data in host init response
		@type message: scalarizr.messaging.Message
		@param message: HostInitResponse
		"""
		with bus.initialization_op as op:
			with op.phase(self._phase_redis):
				with op.step(self._step_accept_scalr_conf):
		
					if not message.body.has_key(BEHAVIOUR) or message.db_type != BEHAVIOUR:
						raise HandlerError("HostInitResponse message for %s behaviour must have '%s' property and db_type '%s'" 
										% (BEHAVIOUR, BEHAVIOUR, BEHAVIOUR))
					
					dir = os.path.dirname(self._volume_config_path)
					if not os.path.exists(dir):
						os.makedirs(dir)
					
					redis_data = message.redis.copy()	
					self._logger.info('Got Redis part of HostInitResponse: %s' % redis_data)
					
					for key, file in ((OPT_VOLUME_CNF, self._volume_config_path), 
									(OPT_SNAPSHOT_CNF, self._snapshot_config_path)):
						if os.path.exists(file):
							os.remove(file)
						
						if key in redis_data:
							if redis_data[key]:
								Storage.backup_config(redis_data[key], file)
							del redis_data[key]
					
					self._logger.debug("Update redis config with %s", redis_data)
					self._update_config(redis_data)
					
					self.redis.is_replication_master = self.is_replication_master
					self.redis.persistence_type = self.persistence_type 


	def on_before_host_up(self, message):
		"""
		Configure redis behaviour
		@type message: scalarizr.messaging.Message		
		@param message: HostUp message
		"""

		repl = 'master' if self.redis.is_replication_master else 'slave'
		
		if self.redis.is_replication_master:
			self._init_master(message)									  	
		else:
			self._init_slave(message)			
		bus.fire('service_configured', service_name=SERVICE_NAME, replication=repl)
					
					
	def on_before_reboot_start(self, *args, **kwargs):
		if self.redis.service.running:
				self.redis.redis_cli.save()
				self.redis.service.stop('rebooting')


	def on_before_reboot_finish(self, *args, **kwargs):
		self._insert_iptables_rules()		


	def on_BeforeHostTerminate(self, message):
		self._logger.info('Handling BeforeHostTerminate message from %s' % message.local_ip)
		if message.local_ip == self._platform.get_private_ip():
			if self.redis.service.running:
				self._logger.info('Dumping redis data on disk')
				self.redis.redis_cli.save()
				self._logger.info('Stopping %s service' % BEHAVIOUR)
				self.redis.service.stop('Server will be terminated')
			if not self.is_replication_master:
				self._logger.info('Destroying volume %s' % self.storage_vol.id)
				self.storage_vol.destroy(remove_disks=True)
				self._logger.info('Volume %s was destroyed.' % self.storage_vol.id)
	
	
	def on_DbMsr_CreateDataBundle(self, message):
		
		try:
			op = operation(name=self._op_data_bundle, phases=[{
				'name': self._phase_data_bundle, 
				'steps': [self._step_create_data_bundle]
			}])
			op.define()

			
			with op.phase(self._phase_data_bundle):
				with op.step(self._step_create_data_bundle):
			
					bus.fire('before_%s_data_bundle' % BEHAVIOUR)
					# Creating snapshot		
					snap = self._create_snapshot()
					used_size = int(system2(('df', '-P', '--block-size=M', self._storage_path))[0].split('\n')[1].split()[2][:-1])
					bus.fire('%s_data_bundle' % BEHAVIOUR, snapshot_id=snap.id)			
					
					# Notify scalr
					msg_data = dict(
						db_type 	= BEHAVIOUR,
						used_size	= '%.3f' % (float(used_size) / 1000,),
						status		= 'ok'
					)
					msg_data[BEHAVIOUR] = self._compat_storage_data(snap=snap)
					self.send_message(DbMsrMessages.DBMSR_CREATE_DATA_BUNDLE_RESULT, msg_data)

		except (Exception, BaseException), e:
			self._logger.exception(e)
			
			# Notify Scalr about error
			self.send_message(DbMsrMessages.DBMSR_CREATE_DATA_BUNDLE_RESULT, dict(
				db_type 	= BEHAVIOUR,
				status		='error',
				last_error	= str(e)
			))
			
	
	def on_DbMsr_PromoteToMaster(self, message):
		"""
		Promote slave to master
		@type message: scalarizr.messaging.Message
		@param message: redis_PromoteToMaster
		"""
		
		if message.db_type != BEHAVIOUR:
			self._logger.error('Wrong db_type in DbMsr_PromoteToMaster message: %s' % message.db_type)
			return
		
		if self.redis.is_replication_master:
			self._logger.warning('Cannot promote to master. Already master')
			return
		bus.fire('before_slave_promote_to_master')
		
		master_storage_conf = message.body.get('volume_config')
		tx_complete = False	
		old_conf 		= None
		new_storage_vol	= None	
		
		try:
			msg_data = dict(
					db_type=BEHAVIOUR, 
					status="ok",
			)
			
			if master_storage_conf:

				self.redis.service.stop('Unplugging slave storage and then plugging master one')

				old_conf = self.storage_vol.detach(force=True) # ??????
				new_storage_vol = self._plug_storage(self._storage_path, master_storage_conf)	
							
				# Continue if master storage is a valid redis storage 
				if not self.redis.working_directory.is_initialized(self._storage_path):
					raise HandlerError("%s is not a valid %s storage" % (self._storage_path, BEHAVIOUR))
				
				Storage.backup_config(new_storage_vol.config(), self._volume_config_path) 
				msg_data[BEHAVIOUR] = self._compat_storage_data(vol=new_storage_vol)
				
			self.redis.init_master(self._storage_path, password=self.redis.password)
			self._update_config({OPT_REPLICATION_MASTER : "1"})
				
			if not master_storage_conf:
									
				snap = self._create_snapshot()
				Storage.backup_config(snap.config(), self._snapshot_config_path)
				msg_data[BEHAVIOUR] = self._compat_storage_data(self.storage_vol, snap)
				
			self.send_message(DbMsrMessages.DBMSR_PROMOTE_TO_MASTER_RESULT, msg_data)	
								
			tx_complete = True
			bus.fire('slave_promote_to_master')
			
		except (Exception, BaseException), e:
			self._logger.exception(e)
			if new_storage_vol:
				new_storage_vol.detach()
			# Get back slave storage
			if old_conf:
				self._plug_storage(self._storage_path, old_conf)
			
			self.send_message(DbMsrMessages.DBMSR_PROMOTE_TO_MASTER_RESULT, dict(
				db_type=BEHAVIOUR, 															
				status="error",
				last_error=str(e)
			))

			# Start redis
			self.redis.service.start()
		
		if tx_complete and master_storage_conf:
			# Delete slave EBS
			self.storage_vol.destroy(remove_disks=True)
			self.storage_vol = new_storage_vol
			Storage.backup_config(self.storage_vol.config(), self._volume_config_path)



	def on_DbMsr_NewMasterUp(self, message):
		"""
		Switch replication to a new master server
		@type message: scalarizr.messaging.Message
		@param message:  DbMsr__NewMasterUp
		"""
		if not message.body.has_key(BEHAVIOUR) or message.db_type != BEHAVIOUR:
			raise HandlerError("DbMsr_NewMasterUp message for %s behaviour must have '%s' property and db_type '%s'" % 
							BEHAVIOUR, BEHAVIOUR, BEHAVIOUR)
		
		if self.redis.is_replication_master:
			self._logger.debug('Skipping NewMasterUp. My replication role is master')	
			return 

		host = message.local_ip or message.remote_ip
		self._logger.info("Switching replication to a new %s master %s"% (BEHAVIOUR, host))
		bus.fire('before_%s_change_master' % BEHAVIOUR, host=host)			
		
		password = self._get_password()	
		self.redis.init_slave(self._storage_path, host, DEFAULT_PORT, password)
		self.redis.wait_for_sync()
			
		self._logger.debug("Replication switched")
		bus.fire('%s_change_master' % BEHAVIOUR, host=host)
			
	
	def on_DbMsr_CreateBackup(self, message):
		tmpdir = backup_path = None
		try:
			op = operation(name=self._op_backup, phases=[{
				'name': self._phase_backup, 
				'steps': [self._step_copy_database_file, 
						self._step_upload_to_cloud_storage]
			}])
			op.define()			
			
			
			with op.phase(self._phase_backup):

				with op.step(self._step_copy_database_file):
			
					# Dump all databases
					self._logger.info("Dumping all databases")			
					tmpdir = tempfile.mkdtemp()		
					src_path = self.redis.db_path
					dump_path = os.path.join(tmpdir, os.path.basename(self.redis.db_path))
					
					if not os.path.exists(src_path):
						raise BaseException('%s DB file %s does not exist. Skipping Backup process' % (BEHAVIOUR, src_path))
					
					# Defining archive name and path
					backup_filename = time.strftime('%Y-%m-%d-%H:%M:%S')+'.tar.gz'
					backup_path = os.path.join('/tmp', backup_filename)
		
					shutil.copyfile(src_path, dump_path)
					rchown('redis', tmpdir)
					
					# Creating archive 
					backup = tarfile.open(backup_path, 'w:gz')
					backup.add(dump_path, os.path.basename(self.redis.db_path))
					backup.close()
				
					# Creating list of full paths to archive chunks
					if os.path.getsize(backup_path) > BACKUP_CHUNK_SIZE:
						parts = [os.path.join(tmpdir, file) for file in split(backup_path, backup_filename, BACKUP_CHUNK_SIZE , tmpdir)]
					else:
						parts = [backup_path]
					
				with op.step(self._step_upload_to_cloud_storage):
					
					cloud_storage_path = self._platform.scalrfs.backups(BEHAVIOUR)
					self._logger.info("Uploading backup to cloud storage (%s)", cloud_storage_path)
					trn = transfer.Transfer()
					result = trn.upload(parts, cloud_storage_path)
					self._logger.info("%s backup uploaded to cloud storage under %s/%s" % 
								(BEHAVIOUR, cloud_storage_path, backup_filename))
			
			op.ok()
				
			# Notify Scalr
			self.send_message(DbMsrMessages.DBMSR_CREATE_BACKUP_RESULT, dict(
				db_type = BEHAVIOUR,
				status = 'ok',
				backup_parts = result
			))
			
			
						
		except (Exception, BaseException), e:
			self._logger.exception(e)
			
			# Notify Scalr about error
			self.send_message(DbMsrMessages.DBMSR_CREATE_BACKUP_RESULT, dict(
				db_type = BEHAVIOUR,
				status = 'error',
				last_error = str(e)
			))
			
		finally:
			if tmpdir:
				shutil.rmtree(tmpdir, ignore_errors=True)
			if backup_path and os.path.exists(backup_path):
				os.remove(backup_path)				
		
							
	def _init_master(self, message):
		"""
		Initialize redis master
		@type message: scalarizr.messaging.Message 
		@param message: HostUp message
		"""
		
		with bus.initialization_op as op:
			with op.step(self._step_create_storage):		
		
				self._logger.info("Initializing %s master" % BEHAVIOUR)
				
				# Plug storage
				volume_cnf = Storage.restore_config(self._volume_config_path)
				try:
					snap_cnf = Storage.restore_config(self._snapshot_config_path)
					volume_cnf['snapshot'] = snap_cnf
				except IOError:
					pass
				self.storage_vol = self._plug_storage(mpoint=self._storage_path, vol=volume_cnf)
				Storage.backup_config(self.storage_vol.config(), self._volume_config_path)	
			
			with op.step(self._step_init_master):
				password = self._get_password()
				self.redis.init_master(mpoint=self._storage_path, password=password)
			
				msg_data = dict()
				msg_data.update({OPT_REPLICATION_MASTER 		: 	'1',
									OPT_MASTER_PASSWORD			:	self.redis.password})	
				
			with op.step(self._step_create_data_bundle):
				# Create snapshot
				snap = self._create_snapshot()
				Storage.backup_config(snap.config(), self._snapshot_config_path)
		
			with op.step(self._step_collect_host_up_data):
				# Update HostUp message 
				msg_data.update(self._compat_storage_data(self.storage_vol, snap))
					
				if msg_data:
					message.db_type = BEHAVIOUR
					message.redis = msg_data.copy()
					try:
						del msg_data[OPT_SNAPSHOT_CNF], msg_data[OPT_VOLUME_CNF]
					except KeyError:
						pass 
					self._update_config(msg_data)		

	def _get_password(self):
		password = None 
		if self._cnf.rawini.has_option(CNF_SECTION, OPT_MASTER_PASSWORD):
			password = self._cnf.rawini.get(CNF_SECTION, OPT_MASTER_PASSWORD)	
		return password		
	
	def _get_master_host(self):
		master_host = None
		self._logger.info("Requesting master server")
		while not master_host:
			try:
				master_host = list(host 
					for host in self._queryenv.list_roles(self._role_name)[0].hosts 
					if host.replication_master)[0]
			except IndexError:
				self._logger.debug("QueryEnv respond with no %s master. " % BEHAVIOUR + 
						"Waiting %d seconds before the next attempt" % 5)
				time.sleep(5)
		return master_host

				
	def _init_slave(self, message):
		"""
		Initialize redis slave
		@type message: scalarizr.messaging.Message 
		@param message: HostUp message
		"""
		self._logger.info("Initializing %s slave" % BEHAVIOUR)
		
		with bus.initialization_op as op:
			with op.step(self._step_create_storage):
				
				self._logger.debug("Initializing slave storage")
				self.storage_vol = self._plug_storage(self._storage_path, 
						dict(snapshot=Storage.restore_config(self._snapshot_config_path)))			
				Storage.backup_config(self.storage_vol.config(), self._volume_config_path)
					

				'''
				#cleaning volume
				if self.redis.working_directory.is_initialized(self._storage_path):
					self.redis.working_directory.empty()
				'''

			with op.step(self._step_init_slave):				
				# Change replication master 
				master_host = self._get_master_host()
						
				self._logger.debug("Master server obtained (local_ip: %s, public_ip: %s)",
						master_host.internal_ip, master_host.external_ip)
				
				host = master_host.internal_ip or master_host.external_ip
				self.redis.init_slave(self._storage_path, host, DEFAULT_PORT, self._get_password())
				op.progress(50)
				self.redis.wait_for_sync()
			
			with op.step(self._step_collect_host_up_data):
				# Update HostUp message
				message.redis = self._compat_storage_data(self.storage_vol)
				message.db_type = BEHAVIOUR


	def _update_config(self, data): 
		#XXX: I just don't like it
		#ditching empty data
		updates = dict()
		for k,v in data.items():
			if v: 
				updates[k] = v
		
		self._cnf.update_ini(BEHAVIOUR, {CNF_SECTION: updates})


	def _plug_storage(self, mpoint, vol):
		if not isinstance(vol, Volume):
			vol['tags'] = self.redis_tags
			vol = Storage.create(vol)

		try:
			if not os.path.exists(mpoint):
				os.makedirs(mpoint)
			if not vol.mounted():
				vol.mount(mpoint)
		except StorageError, e:
			if 'you must specify the filesystem type' in str(e):
				vol.mkfs()
				vol.mount(mpoint)
			else:
				raise
		return vol


	def _create_snapshot(self):
		
		system2('sync', shell=True)
		# Creating storage snapshot
		snap = self._create_storage_snapshot()
			
		wait_until(lambda: snap.state in (Snapshot.CREATED, Snapshot.COMPLETED, Snapshot.FAILED))
		if snap.state == Snapshot.FAILED:
			raise HandlerError('%s storage snapshot creation failed. See log for more details' % BEHAVIOUR)
		
		self._logger.info('Redis data bundle created\n  snapshot: %s', snap.id)
		return snap


	def _create_storage_snapshot(self):
		if self.redis.service.running:
			self._logger.info("Dumping Redis data on disk")
			self.redis.redis_cli.save()
		self._logger.info("Creating storage snapshot")
		try:
			return self.storage_vol.snapshot(tags=self.redis_tags)
		except StorageError, e:
			self._logger.error("Cannot create %s data snapshot. %s", (BEHAVIOUR, e))
			raise
		

	def _compat_storage_data(self, vol=None, snap=None):
		ret = dict()
		if vol:
			ret['volume_config'] = vol.config()
		if snap:
			ret['snapshot_config'] = snap.config()
		return ret
	
	def _insert_iptables_rules(self):
		iptables = IpTables()
		if iptables.enabled():
			iptables.insert_rule(None, RuleSpec(dport=DEFAULT_PORT, jump='ACCEPT', protocol=P_TCP))		
	

class RedisCnfController(CnfController):

	def __init__(self):
		cnf_path = UBUNTU_CONFIG_PATH if disttool.is_ubuntu() else CENTOS_CONFIG_PATH
		CnfController.__init__(self, BEHAVIOUR, cnf_path, 'redis', {'1':'yes', '0':'no'})


	@property
	def _software_version(self):
		return software.software_info('redis').version
	
	
	def _get_password(self):
		password = None 
		cnf = bus.cnf
		if cnf.rawini.has_option(CNF_SECTION, OPT_MASTER_PASSWORD):
			password = cnf.rawini.get(CNF_SECTION, OPT_MASTER_PASSWORD)	
		return password
	
	
	def _after_apply_preset(self):
		password = self._get_password()
		cli = RedisCLI(password)
		cli.bgsave()

	'''
	#If we don't need delete from current config file
	# scalr presets options  == default preset params 
	def apply_preset(self, preset):
		conf = Configuration(self._config_format)
		conf.read(self._config_path)
		
		self._before_apply_preset()
		
		ver = self._software_version
		for opt in self._manifest:
			path = opt.name if not opt.section else '%s/%s' % (opt.section, opt.name)
			
			try:
				value = conf.get(path)
			except NoPathError:
				value = ''
			
			if opt.name in preset.settings:
				new_value = preset.settings[opt.name]
				
				# Skip unsupported
				if ver and opt.supported_from and opt.supported_from > ver:
					self._logger.debug("Skipping option '%s' supported from %s; installed %s" % 
							(opt.name, opt.supported_from, ver))
					continue
								
				if not opt.default_value:
					self._logger.debug("Option '%s' has no default value" % opt.name)
				elif new_value == opt.default_value and new_value != value: 
					self._logger.debug("Option '%s' equal to default." % opt.name)
				elif new_value == opt.default_value and new_value == value:
					self._logger.debug("Skip option '%s' equal to default. Not changed" % opt.name)
					continue
				
				if self.definitions and new_value in self.definitions:
					manifest = Configuration('ini')
					if os.path.exists(self._manifest_path):
						manifest.read(self._manifest_path)
					try:
						if manifest.get('%s/type' % opt.name) == 'boolean':
							new_value = self.definitions[new_value]
					except NoPathError, e:
						pass
				
				self._logger.debug("Check that '%s' value changed:'%s'='%s'"%(opt.name, value, new_value))
					
				if new_value == value:
					self._logger.debug("Skip option '%s'. Not changed" % opt.name)
					pass
				else:
					self._logger.debug("Set option '%s' = '%s'" % (opt.name, new_value))
					self._logger.debug('Set path %s = %s', path, new_value)
					conf.set(path, new_value, force=True)
					self._after_set_option(opt, new_value)
			else:
				if value:
					self._logger.debug("Removing option '%s'. Not found in preset" % opt.name)	
					conf.remove(path)
				self._after_remove_option(opt)
		
		self._after_apply_preset()
		conf.write(self._config_path)
		'''

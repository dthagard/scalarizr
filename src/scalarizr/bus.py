'''
Created on Apr 6, 2010

@author: marat
'''

from scalarizr.util import Observable
from optparse import OptionParser

class Bus(Observable):
	base_path = None
	"""
	@ivar string: Application base path
	"""
	
	etc_path = None
	"""
	@ivar string: Application etc path 
	"""
	
	config = None
	"""
	@ivar ConfigParser.ConfigParser: Configuration (config.ini and includes) 
	"""
	
	optparser = OptionParser()
	"""
	@ivar optparse.OptionParser: Command line options
	"""
	
	db = None
	"""
	@ivar sqlalchemy.pool.SingletonThreadPool: Database connection pool
	"""
	
	messaging_service = None
	"""
	@ivar scalarizr.messaging.MessageService: Default message service 
	"""
	
	queryenv_service = None
	"""
	@ivar scalarizr.queryenv.QueryEnv:  QueryEnv service client
	"""
	
	platfrom = None
	"""
	@ivar scalarizr.platform.Platform: Platform (ec2, rs, vps...)
	"""

bus = Bus()
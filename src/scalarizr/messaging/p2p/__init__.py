'''
Created on Dec 5, 2009

@author: marat
'''

from scalarizr.bus import bus
from scalarizr.messaging import MessageService, Message, MetaOptions, MessagingError
import logging
from scalarizr.util import configtool


class P2pOptions:
	SERVER_ID = "p2p_server_id"
	CRYPTO_KEY_PATH = "p2p_crypto_key_path"
	PRODUCER_ENDPOINT = "p2p_producer_endpoint"
	CONSUMER_ENDPOINT = "p2p_consumer_endpoint"

class P2pMessageService(MessageService):
	_config = {}
	_consumer = None
	_producer = None
	
	def __init__(self, config):
		self._config = config

	def new_message(self, name=None, meta={}, body={}):
		return P2pMessage(name, meta, body)
	
	def get_consumer(self):
		if self._consumer is None:
			import consumer
			self._consumer = consumer.P2pMessageConsumer(self._config)
		return self._consumer
	
	def get_producer(self):
		if self._producer is None:
			import producer
			self._producer = producer.P2pMessageProducer(self._config)
		return self._producer

def new_service(config):
	return P2pMessageService(config)
	
class _P2pBase(object):
	server_id = None
	crypto_key = None
	
	def __init__(self, config):
		for pair in config:
			key = pair[0]
			if key == P2pOptions.SERVER_ID:
				self.server_id = pair[1]
			elif key == P2pOptions.CRYPTO_KEY_PATH:
				self.crypto_key = pair[1]

		config = bus.config
		if self.server_id is None:
			self.server_id = config.get(configtool.SECT_GENERAL, configtool.OPT_SERVER_ID)
		if self.crypto_key is None:
			self.crypto_key = configtool.read_key(
					config.get(configtool.SECT_GENERAL, configtool.OPT_CRYPTO_KEY_PATH))
	
class _P2pMessageStore:
	_logger = None

	def __init__(self):
		self._logger = logging.getLogger(__name__)
	
	def _conn(self):
		db = bus.db
		return db.get().get_connection()
		
	def put_ingoing(self, message, queue):
		conn = self._conn()
		cur = conn.cursor()
		try:
			sql = """INSERT INTO p2p_message (id, message, message_id, 
						message_name, queue, is_ingoing, in_is_handled)
					VALUES 
						(NULL, ?, ?, ?, ?, ?, ?)"""
			cur.execute(sql, [str(message), message.id, message.name, queue, 1, 0])
			
			if message.meta.has_key(MetaOptions.REQUEST_ID):
				cur.execute("""UPDATE p2p_message 
						SET response_uuid = ? WHERE message_id = ?""", 
						[message.id, message.meta[MetaOptions.REQUEST_ID]])
				
			conn.commit()
		finally:
			cur.close()
			
	def get_unhandled(self):
		"""
		Return list of unhandled messages in obtaining order
		@return: [(queue, message), ...]   
		"""
		cur = self._conn().cursor()
		try:
			cur.execute("""SELECT queue, message_id FROM p2p_message
					WHERE is_ingoing = 1 AND in_is_handled = 0 ORDER BY id""")
			ret = []
			for r in cur.fetchall():
				ret.append((r["queue"], self.load(r["message_id"], True)))
			return ret
		finally:
			cur.close()
	
	def mark_as_handled(self, message_id):
		conn = self._conn()
		cur = conn.cursor()
		try:
			sql = """UPDATE p2p_message SET in_is_handled = ? 
					WHERE message_id = ? AND is_ingoing = ?"""
			cur.execute(sql, [1, message_id, 1])
			conn.commit()
		finally:
			cur.close()

	def put_outgoing(self, message, queue):
		conn = self._conn()
		cur = conn.cursor()
		try:
			sql = """INSERT INTO p2p_message (id, message, message_id, message_name, queue, 
						is_ingoing, out_is_delivered, out_delivery_attempts) 
					VALUES 
						(NULL, ?, ?, ?, ?, ?, ?, ?)"""
			cur.execute(sql, [str(message), message.id, message.name, queue, 0, 0, 0])
			conn.commit()
		finally:
			cur.close()
			
	def get_undelivered (self):
		"""
		Return list of undelivered messages in outgoing order
		"""
		cur = self._conn().cursor()
		try:
			cur.execute("""SELECT queue, message_id FROM p2p_message
					WHERE is_ingoing = 0 AND out_is_delivered = 0 ORDER BY id""")
			ret = []
			for r in cur.fetchall():
				ret.append((r[0], self.load(r[1], False)))
			return ret
		finally:
			cur.close()
			
	def mark_as_delivered(self, message_id):
		return self._mark_as_delivered(message_id, 1)
	
	def mark_as_undelivered(self, message_id):
		return self._mark_as_delivered(message_id, 0)

	def _mark_as_delivered (self, message_id, delivered):
		conn = self._conn()
		cur = conn.cursor()
		try:
			sql = """UPDATE p2p_message SET out_delivery_attempts = out_delivery_attempts + 1, 
						out_last_attempt_time = datetime('now'), out_is_delivered = ? 
					WHERE 
						message_id = ? AND is_ingoing = ?"""
			cur.execute(sql, [int(bool(delivered)), message_id, 0])
			conn.commit()
		finally:
			cur.close()
			
	def load(self, message_id, is_ingoing):
		cur = self._conn().cursor()
		try:
			cur.execute("""SELECT * FROM p2p_message 
					WHERE message_id = ? AND is_ingoing = ?""", 
					[message_id, int(bool(is_ingoing))])
			row = cur.fetchone()
			if not row is None:
				message = P2pMessage()
				self._unmarshall(message, row)
				return message
			else:
				raise MessagingError("Cannot find message (message_id: %s)" % (message_id))
		finally:
			cur.close()
	
	def is_handled(self, message_id):
		cur = self._conn().cursor()
		try:
			cur.execute("""SELECT in_is_handled FROM p2p_message 
					WHERE message_id = ? AND is_ingoing = 1""", 
					[message_id])
			return cur.fetchone()["in_is_handled"] == 1
		finally:
			cur.close()
	
	def is_delivered(self, message_id):
		cur = self._conn().cursor()
		try:
			cur.execute("""SELECT is_delivered FROM p2p_message
					"WHERE message_id = ? AND is_ingoing = ?""", 
					[message_id, 0])
			return cur.fetchone()["out_is_delivered"] == 1
		finally:
			cur.close()
		
	def is_response_received(self, message_id):
		cur = self._conn().cursor()
		try:
			sql = """SELECT response_id FROM p2p_message 
					WHERE message_id = ? AND is_ingoing = ?"""
			cur.execute(sql, [message_id, 0])
			return cur.fetchone()["response_id"] != ""
		finally:
			cur.close()
		
	def get_response(self, message_id):
		cur = self._conn().cursor()
		try:
			cur.execute("""SELECT response_id FROM p2p_message 
					WHERE message_id = ? AND is_ingoing = ?""", 
					[message_id, 0])
			response_id = cur.fetchone()["response_id"]
			if not response_id is None:
				return self.load(response_id, True)
			return None
		finally:
			cur.close()
		
	def _unmarshall(self, message, row):
		message.fromxml(row["message"])
		
	def _marshall(self, message, row={}):
		row["message_id"] = message.id
		row["message_name"] = message.name
		row["message"] = str(message)
		return row

_message_store = None
def P2pMessageStore():
	global _message_store
	if _message_store is None:
		_message_store = _P2pMessageStore()
	return _message_store

class P2pMessage(Message):

	def __init__(self, name=None, meta={}, body={}):
		Message.__init__(self, name, meta, body)
		self.__dict__["_store"] = P2pMessageStore()
	
	def is_handled(self):
		return self._store.is_handled(self.id)
	
	def is_delivered(self):
		return self._store.is_delivered(self.id)
	
	def is_responce_received(self):
		return self._store.is_response_received(self.id)
		
	def get_response(self):
		return self._store.get_response(self.id)

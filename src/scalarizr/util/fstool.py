'''
Created on May 10, 2010

@author: marat
'''

from scalarizr.util import disttool, system
import re
import os

class FstoolError(BaseException):
	NO_FS = -100
	CANNOT_MOUNT = -101
	
	message = None
	code = None
	
	def __init__(self, *args):
		BaseException.__init__(self, *args)
		self.message = args[0]
		try:
			self.code = args[1]
		except IndexError:
			pass


class Fstab:
	"""
	Wrapper over /etc/fstab
	"""
	LOCATION = None
	filename = None	
	_entries = None
	_re = None
	
	def __init__(self, filename=None):
		self.filename = filename if not filename is None else self.LOCATION
		self._entries = None
		self._re = re.compile("^(\\S+)\\s+(\\S+)\\s+(\\S+)\\s+(\\S+).*$")
		
	def list_entries(self, rescan=False):
		if not self._entries or rescan:
			self._entries = []
			f = open(self.filename, "r")
			for line in f:
				if line[0:1] == "#":
					continue
				m = self._re.match(line)
				if m:
					self._entries.append(TabEntry(
						m.group(1), m.group(2), m.group(3), m.group(4), line.strip()
					))
			f.close()
			
		return list(self._entries)
	
	def append(self, entry):
		line = "\n" + "\t".join([entry.device, entry.mpoint, entry.fstype, entry.options])
		try:
			f = open(self.filename, "a")
			f.write(line)
		finally:
			f.close()
			
	def contains(self, devname=None, mpoint=None, rescan=False):
		for entry in self.list_entries(rescan):
			return any(bool(mpoint and entry.mpoint == mpoint) or bool(devname and entry.device == devname) \
					for entry in self.list_entries(rescan))
		
	def find(self, devname=None, mpoint=None, fstype=None, rescan=False):
		ret = list(entry for entry in self.list_entries(rescan) if \
				(devname and entry.device == devname) or \
				(mpoint and entry.mpoint == mpoint) or \
				(fstype and entry.fstype == fstype))
		return ret
	

class Mtab(Fstab):
	"""
	Wrapper over /etc/mtab
	"""
	LOCAL_FS_TYPES = None	

		
class TabEntry(object):
	device = None
	mpoint = None
	fstype = None
	options = None	
	value = None
	
	def __init__(self, device, mpoint, fstype, options, value=None):
		self.device = device
		self.mpoint = mpoint
		self.fstype = fstype
		self.options = options		
		self.value = value
		
	def __str__(self):
		return "%s %s %s %s" % (self.device, self.mpoint, self.fstype, self.options)

		
if disttool.is_linux():
	Fstab.LOCATION = "/etc/fstab"	
	Mtab.LOCATION = "/etc/mtab"
	Mtab.LOCAL_FS_TYPES = ('ext2', 'ext3', 'xfs', 'jfs', 'reiserfs', 'tmpfs', 'sysfs', 'proc')
	
elif disttool.is_sun():
	Fstab.LOCATION = "/etc/vfstab"	
	Mtab.LOCATION = "/etc/mnttab"
	Mtab.LOCAL_FS_TYPES = ('ext2', 'ext3', 'xfs', 'jfs', 'reiserfs', 'tmpfs', 
		'ufs', 'sharefs', 'dev', 'devfs', 'ctfs', 'mntfs',
		'proc', 'lofs',   'objfs', 'fd', 'autofs')
	
"""
def mount (device, mpoint, options=()):
	if not os.path.exists(mpoint):
		os.makedirs(mpoint)
	
	options = " ".join(options) 
	out = system("mount %(options)s %(device)s %(mpoint)s 2>&1" % vars())[0]
	if out.find("you must specify the filesystem type") != -1:
		raise FstoolError("No filesystem found on device '%s'" % (device), FstoolError.NO_FS)
	
	mtab = Mtab()
	if not mtab.contains(device):
		raise FstoolError("Cannot mount device '%s'. %s" % (device, out), FstoolError.CANNOT_MOUNT)
"""

def mount (device, mpoint = '/mnt', options=(), make_fs = False, auto_mount = False, fstype='ext3'):
	if not os.path.exists(mpoint):
		os.makedirs(mpoint)
	
	options = " ".join(options) 
	
	if make_fs:
		mkfs(device,fstype)
			
	out = system("mount %(options)s %(device)s %(mpoint)s 2>&1" % vars())[0]
	if out.find("you must specify the filesystem type") != -1:
		raise FstoolError("No filesystem found on device '%s'" % (device), FstoolError.NO_FS)
		
	mtab = Mtab()
	if not mtab.contains(device):
		raise FstoolError("Cannot mount device '%s'. %s" % (device, out), FstoolError.CANNOT_MOUNT)
	
	if auto_mount:
		fstab = Fstab()
		if not fstab.contains(device, mpoint = mpoint, rescan=True):
			fstab.append(TabEntry(device, mpoint, "auto", "defaults\t0\t0"))

def umount(device, options=(), clean_fstab = False):
	if not os.path.exists(device):
		raise FstoolError("Device %s not found" % (device), FstoolError.CANNOT_UMOUNT)
	
	options = " ".join(options)
	
	returncode = system("umount %(options)s %(device)s 2>&1" % vars())[2]
	if returncode :
		raise FstoolError("Cannot unmount device '%s'" % (device), FstoolError.CANNOT_UMOUNT)
	
	if clean_fstab:
		fstab = None
		
		try:
			fstab_file = open(Fstab.LOCATION, 'r')
		except OSError:
			pass
		else:
			fstab = fstab_file.read()
		finally:
			fstab_file.close()
		
		if fstab:
			fstab = re.sub(r"\n" + device + ".*\n", '\n', fstab)
			
			try:
				fstab_file = open(Fstab.LOCATION, 'w')
			except OSError:
				pass
			else:
				fstab = fstab_file.write(fstab)
			finally:
				fstab_file.close()
	
	
def mkfs(device, fstype = 'ext3'):
	_out, _err, _retcode = system("/sbin/mkfs -t " + fstype + " -F " + device)
	if _retcode:
		raise FstoolError("Cannot create file system on device '%s'. %s" % (device, _err), FstoolError.CANNOT_CREATE_FS)
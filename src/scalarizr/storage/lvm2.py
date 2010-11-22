'''
Created on Nov 11, 2010

@author: Dmytro Korsakov
'''

from scalarizr.util import system

class Lvm2Error(BaseException):
	pass

class Lvm2:
	'''
	object-oriented interface to lvm2
	'''
	group = None
	
	def __init__(self, group='vg'):
		self.group = group
		
	def _parse_table(self, cmd='lvs'):
		full_path = '/sbin/%s' % cmd
		if not os.
		return [i.strip().split('|') for i in 
				system([full_path, '--separator', "|"], shell=False)[0].split('\n')[1:-1]]		
		
		
		
		
		
		
		
		
		
		
		
		
	
	def add_physical_volumes(self, *args):
		e = system(['pvcreate'] + list(args))[1]	
		if e:
			raise Lvm2Error(e)
	
	def remove_physical_volume(self, name, group=None):
		if not group: group = self.group
		system(['pvmove ', name])
		system(['vgreduce', group, name])
		system(['pvremove ', '-f', name])
		
	def get_pv_info(self):
		return [i.strip().split('|') for i in 
				system(['/sbin/pvs', '--separator', "|"], shell=False)[0].split('\n')[1:-1]]
		
	def get_physical_volumes(self):
		pvs = self.get_pv_info()
		if pvs:
			return [j[0] for j in pvs]
		
	
	def create_volume_group(self, group, block_size, *args):
		if not group: group = self.group
		if not block_size: block_size = '16M'
		print ['/sbin/vgcreate', '-s', block_size, group] + list(args)
		system(['/sbin/vgcreate', '-s', block_size, group] + list(args),  shell=False)
	
	def extend_group(self,group=None, *args):
		if not group: group = self.group
		system(['vgextend', group] + list(args))	
		
	def remove_volume_group(self, group=None):
		if not group: group = self.group
		system(['vgremove', group])
	
	def get_volume_groups_info(self):
		#TODO: parse output
		return [i.split() for i in system(['vgs'])[0].split('\n')[1:-1]]
	
	def get_logic_volume_size(self, lv_name):
		lv_info = self.get_logic_volumes_info()
		if lv_info:
			for lv in lv_info:
				if lv[0] == lv_name:
					return lv[3]
		return 0
	
	def get_volume_groups(self):
		vgs = self.get_volume_groups_info()
		if vgs:
			return [j[0] for j in vgs]
			
	
	def get_logic_volumes_info(self):
		return [i.strip().split('|') for i in 
				system(['/sbin/lvs', '--separator', "|"], shell=False)[0].split('\n')[1:-1]]
		
	def get_volume_group(self, device):
		lvs_info = self.get_logic_volumes_info()
		if lvs_info:
			for volume in lvs_info:
				if device.endswith(volume[0]):
					return volume[1]
		return None
	
	def get_vg_free_space(self, group_name):
		for group in self.get_volume_group_list():
			if group[0]==group_name:
				return group[-1]
		return 0
	
	def create_logic_volume(self, volume_name, size, group=None):
		if not group: group = self.group
		system(['lvcreate', '-n', volume_name, '-L', size, group])
		
	def create_snapshot_volume(self, volume_name, buf_size, l_volume, group=None):	
		if not group: group = self.group
		system(['lvcreate', '-s', '-n', volume_name, '-L', buf_size, '/dev/%s/%s'%(group,l_volume)])	
	
	def remove_logic_volume(self, group, volume_name):
		if not group: group = self.group
		system(['lvremove', '-f', '%/%' % (group, volume_name)])
			
	def repair_group(self, group):
		if not group: group = self.group
		system(['vgreduce', '--removemissing', group])
		system('vgchange', '-a', 'y', group)
		
	def dm_mod(self):
		'''
		Ubuntu 8 needs add dm_mod to kernel manually
		'''
		#modprobe dm_mod 
		system('modprobe', 'dm_mod')
	
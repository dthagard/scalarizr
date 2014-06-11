import logging
import os
import shutil
import sys
import time
import subprocess
import pprint

from boto.ec2.blockdevicemapping import BlockDeviceType
from boto.ec2.blockdevicemapping import BlockDeviceMapping

from scalarizr import linux
from scalarizr import util
from scalarizr.api.image import ImageAPIDelegate
from scalarizr.api.image import ImageAPIError
from scalarizr.config import ScalarizrCnf
from scalarizr.linux import coreutils
from scalarizr.linux import mount
from scalarizr.linux import rsync
from scalarizr.linux import pkgmgr
from scalarizr.node import __node__
from scalarizr.node import base_dir as etc_dir
from scalarizr.node import private_dir
from scalarizr.storage2 import filesystem
from scalarizr.storage2 import volume as create_volume
from scalarizr.storage2.util import loop
from scalarizr.util import system2


LOG = logging.getLogger(__name__)


class InstanceStoreImageMaker(object):
    
    def __init__(self,
        image_name,
        image_size,
        delegate,
        excludes=[],
        bucket_name=None,
        destination='/mnt/scalr_image'):

        self.image_name = image_name
        self.image_size = image_size
        self.environ = delegate.environ
        self.credentials = delegate.credentials
        self.ami_bin_dir = delegate.ami_bin_dir
        self.excludes = excludes
        self.bucket_name = bucket_name
        self.destination = destination
        self.platform = __node__['platform']

        if not excludes:
            self.excludes = [
                # self.destination,
                ]

    def prepare_image(self):
        # prepares image with ec2-bundle-vol command
        cmd = (
            os.path.join(self.ami_bin_dir, 'ec2-bundle-vol'),
            '--cert', self.credentials['cert'],
            '--privatekey', self.credentials['key'],
            '--user', self.credentials['user'],
            '--arch', linux.os['arch'],
            '--size', str(self.image_size),
            '--destination', self.destination,
            # '--exclude', ','.join(self.excludes),
            '--prefix', self.image_name,
            '--volume', '/',
            '--debug')
        LOG.debug('Image prepare command: ' + ' '.join(cmd))
        out = linux.system(cmd, 
            env=self.environ,
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT)[0]
        LOG.debug('Image prepare command out: %s' % out)

    def upload_image(self):
        LOG.debug('Uploading image (with ec2-upload-bundle)')
        manifest = os.path.join(self.destination, self.image_name) + '.manifest.xml'
        bucket = os.path.basename(self.platform.scalrfs.root())
        cmd = (
            os.path.join(self.ami_bin_dir, 'ec2-upload-bundle'),
            '--bucket', bucket,
            '--access-key', self.credentials['access_key'],
            '--secret-key', self.credentials['secret_key'],
            '--manifest', manifest)
        LOG.debug('Image upload command: ', ' '.join(cmd))
        out = linux.system(cmd, env=self.environ)[0]
        LOG.debug('Image upload command out: %s' % out)
        return bucket, manifest

    def register_image(self, bucket, manifest):
        LOG.debug('Registering image')
        s3_manifest_path = '%s/%s' % (bucket, os.path.basename(manifest))
        LOG.debug("Registering image '%s'", s3_manifest_path)

        conn = self.platform.new_ec2_conn()

        instance_id = self.platform.get_instance_id()
        instance = conn.get_all_instances([instance_id])[0].instances[0]
        
        ami_id = conn.register_image(
            name=self.image_name,
            image_location=s3_manifest_path,
            kernel_id=instance.kernel,
            architecture=instance.architecture)

        LOG.debug("Image is registered.")
        LOG.debug('Image %s is available', ami_id)
        return ami_id

    def cleanup(self):
        # remove image from the server
        linux.system('chmod 755 %s/keys/ec2-*' % private_dir, shell=True)
        linux.system('rm -f %s/keys/ec2-*' % private_dir, shell=True)
        linux.system('rm -f %s/%s.*' % (self.destination, self.image_name), shell=True)

    def create_image(self):
        try:
            self.prepare_image()
            bucket, manifest = self.upload_image()
            image_id = self.register_image(bucket, manifest)
            return image_id
        finally:
            self.cleanup()


class EBSImageMaker(object):

    def __init__(self, image_name, root_disk, delegate, destination='/mnt/scalr_image'):
        self.image_name = image_name
        self.root_disk = root_disk
        self.image_size = int(self.root_disk.size/1024)
        self.environ = delegate.environ
        self.credentials = delegate.credentials
        self.ami_bin_dir = delegate.ami_bin_dir
        self.platform = __node__['platform']
        self.destination = destination
        self.temp_vol = None
        self.excludes = [
                # self.destination,
                ]

    def _assure_space(self):
        """
        Assures that there is enough free space on destination device for image
        """
        avail_space = coreutils.statvfs(self.destination)['avail'] / 1024 / 1024
        if avail_space <= self.image_size:
            os.mkdir('/mnt/temp-vol')
            LOG.debug('Making temp volume')
            self.temp_vol = self.make_volume(self.image_size/1000,
                '/mnt/temp-vol',
                mount=True)
            self.destination = '/mnt/temp-vol'

    def prepare_image(self):
        """Prepares imiage with ec2-bundle-vol command"""
        self._assure_space()
        if not os.path.exists(self.destination):
            os.mkdir(self.destination)
        cmd = (
            os.path.join(self.ami_bin_dir, 'ec2-bundle-vol'),
            '--cert', self.credentials['cert'],
            '--privatekey', self.credentials['key'],
            '--user', self.credentials['user'],
            '--arch', linux.os['arch'],
            '--size', str(self.image_size),
            '--destination', self.destination,
            '--exclude', self.destination,
            '--prefix', self.image_name,
            '--volume', '/',
            '--debug')
        LOG.debug('Image prepare command: ' + ' '.join(cmd))
        out = linux.system(cmd, 
            env=self.environ,
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT)[0]
        LOG.debug('Image prepare command out: %s' % out)

    def make_volume(self, size, mpoint, mount=False):
        ebs_config = {'type': 'ebs',
            'size': size}
        ebs_config['size'] = size
        LOG.debug('Creating ebs volume')
        volume = create_volume(ebs_config, fstype='ext4')
        volume.mpoint = mpoint
        volume.ensure(mount=True, mkfs=True)
        if not mount:
            volume.umount()
        LOG.debug('Volume created %s' % volume.device)
        return volume

    def fix_fstab(self, volume):
        fstab_file_path = os.path.join(volume.mpoint, 'etc/fstab')
        fstab = mount.fstab(fstab_file_path)
        # TODO: remove all ebses

        try:
            del fstab[volume.mpoint]
        except KeyError:
            pass

    def cleanup_ssh_keys(self, homedir):
        filename = os.path.join(homedir, '.ssh/authorized_keys')
        if os.path.exists(filename):
            LOG.debug('Removing Scalr SSH keys from %s', filename)
            fp = open(filename + '.tmp', 'w+')
            for line in open(filename):
                if 'SCALR-ROLESBUILDER' in line:
                    continue
                fp.write(line)
            fp.close()
            os.rename(filename + '.tmp', filename)

    def cleanup_user_activity(self, homedir):
        for name in (".bash_history", ".lesshst", ".viminfo",
            ".mysql_history", ".history", ".sqlite_history"):
            LOG.debug('Removing user activity file %s', name)
            filename = os.path.join(homedir, name)
            if os.path.exists(filename):
                os.remove(filename)

    def clean_snapshot(self, volume):
        LOG.debug('fixing fstab')
        self.fix_fstab(volume)

        homedirs = [os.path.join('/home', userdir) for userdir 
            in os.listdir(volume.mpoint+'/home')] + [volume.mpoint+'/root']
        for homedir in homedirs:
            self.cleanup_ssh_keys(homedir)
            self.cleanup_user_activity(homedir)
        
    def make_snapshot(self, volume):
        prepared_image_path = os.path.join(self.destination, self.image_name)
        LOG.debug('dd image into volume %s' % volume.device)
        coreutils.dd(**{'if': prepared_image_path, 'of': volume.device, 'bs': '8M'})

        volume.mount()
        self.clean_snapshot(volume)
        LOG.debug('detaching volume')
        volume.detach()

        LOG.debug('Making snapshot of volume %s' % volume.device)
        snapshot = volume.snapshot()
        util.wait_until(
                lambda: snapshot.status() == 'completed',
                logger=LOG,
                error_text='EBS snapshot %s wasnt completed' % snapshot.id)
        LOG.debug('Snapshot is made')

        volume.ensure(mount=True)
        return snapshot.id

    def register_image(self, snapshot_id, root_device_name):
        conn = self.platform.new_ec2_conn()
    
        instance_id = self.platform.get_instance_id()
        instance = conn.get_all_instances([instance_id])[0].instances[0]

        root_vol = BlockDeviceType(snapshot_id=snapshot_id)
        block_device_map = BlockDeviceMapping()
        block_device_map[root_device_name] = root_vol
        return conn.register_image(
            name=self.image_name,
            root_device_name=root_device_name,
            block_device_map=block_device_map,
            kernel_id=instance.kernel,
            architecture=instance.architecture)


    def cleanup(self):
        os.removedirs(self.destination)

    def create_image(self):
        volume = None
        try:
            self.prepare_image()
            size = self.image_size / 1000
            volume = self.make_volume(size, '/mnt/img-mnt')
            snapshot_id = self.make_snapshot(volume)
            image_id = self.register_image(snapshot_id, volume.device)
            return image_id
        finally:
            if volume:
                volume.destroy()
            if self.temp_vol:
                self.temp_vol.destroy()
            self.cleanup()


class EC2ImageAPIDelegate(ImageAPIDelegate):

    _tools_dir = '/var/lib/scalr/ec2-tools'
    _ami_tools_name = 'ec2-ami-tools'

    def __init__(self):
        self.image_maker = None
        self.environ = os.environ.copy()
        self.excludes = None
        self.ami_bin_dir = None
        self._prepare_software()

    def _get_version(self, tools_folder_name):
        version = tools_folder_name.split('-')[-1]
        version = tuple(int(x) for x in version.split('.'))
        return version

    def _remove_old_versions(self):
        for item in os.listdir(self._tools_dir):
            if item.startswith(self._ami_tools_name):
                os.removedirs(os.path.join(self._tools_dir, item))

    def _install_support_packages(self):
        pkgmgr.installed('unzip')
        # system2(('curl cache.ruby-lang.org/pub/ruby/1.9/ruby-1.9.3-p545.tar.gz',
        #     '-o', 'ruby193.tar.gz'))
        # system2(('tar xvf ruby193.tar.gz', '-C', '/tmp'))
        # system2(('/tmp/ruby193/configure', '--prefix', self._tools_dir))
        # system2(('make', '-C', '/tmp/ruby193'))
        # system2(('make install', '-C', '/tmp/ruby193'))
        install_script = system2(('curl', '-sSL', 'https://get.rvm.io'),)[0]

        with open('/tmp/rvm_install.sh', 'w') as fp:
            fp.write(install_script)
        os.chmod('/tmp/rvm_install.sh', 0770)
        system2(('/tmp/rvm_install.sh', '-s', 'stable'), shell=True)
        system2(('/usr/local/rvm/bin/rvm install 1.9.3', '--auto-dotfiles'), shell=True)

        ruby_path = None
        for item in os.listdir('/usr/local/rvm/rubies/'):
            if item.startswith('ruby-1.9.3'):
                ruby_path = '/usr/local/rvm/rubies/' + item
                break
        self.environ['PATH'] = self.environ['PATH'] + (':%s/bin' % ruby_path)
        self.environ['MY_RUBY_HOME'] = ruby_path

    def _prepare_software(self):
        if linux.os['family'] == 'Windows':
            # TODO:
            raise ImageAPIError('Windows')
        else:
            system2(('apt-get', 'update'),)
            system2(('wget',
                'http://s3.amazonaws.com/ec2-downloads/ec2-ami-tools.zip',
                '-P',
                '/tmp'),)
            # system2(('wget',
            #     'http://s3.amazonaws.com/ec2-downloads/ec2-api-tools.zip',
            #     '-P',
            #     '/tmp'),)
            if not os.path.exists(self._tools_dir):
                if not os.path.exists(os.path.dirname(self._tools_dir)):
                    os.mkdir(os.path.dirname(self._tools_dir))
                os.mkdir(self._tools_dir)

            self._remove_old_versions()
            self._install_support_packages()

            system2(('unzip', '/tmp/ec2-ami-tools.zip', '-d', self._tools_dir))
            # system2(('unzip', '/tmp/ec2-api-tools.zip', '-d', self._tools_dir))

            os.remove('/tmp/ec2-ami-tools.zip')
            # os.remove('/tmp/ec2-api-tools.zip')

            directory_contents = os.listdir(self._tools_dir)
            self.ami_bin_dir = None
            for item in directory_contents:
                if self.ami_bin_dir:
                    break
                elif item.startswith('ec2-ami-tools'):
                    self.ami_bin_dir = os.path.join(self._tools_dir,
                        os.path.join(item, 'bin'))

            system2(('export', 'EC2_AMITOOL_HOME=%s' % os.path.dirname(self.ami_bin_dir)),
                shell=True)
            # system2(('export', 'EC2_HOME=%s' % os.path.dirname(self.api_bin_dir)),
            #     shell=True)

            pkgmgr.installed('kpartx')

    def _get_root_device_type(self):
        platform = __node__['platform']
        ec2_conn = platform.new_ec2_conn()
        instance_id = platform.get_instance_id()
        try:
            instance = ec2_conn.get_all_instances([instance_id])[0].instances[0]
        except IndexError:
            msg = 'Failed to find instance %s. ' \
                'If you are importing this server, check that you are doing it from the ' \
                'right Scalr environment' % instance_id
            raise ImageAPIError(msg)

        return instance.root_device_type

    def _get_root_disk(self):
        # list of all mounted devices 
        devices = coreutils.df()

        # root device partition like `df(device='/dev/sda2', ..., mpoint='/')
        root_disk = None
        for device in devices:
            if device.mpoint == '/':
                return device
        
        raise ImageAPIError("Can't find root device")

    def _setup_environment(self):
        platform = __node__['platform']
        cnf = ScalarizrCnf(etc_dir)
        cert, pk = platform.get_cert_pk()
        access_key, secret_key = platform.get_access_keys()

        cert_path = cnf.write_key('ec2-cert.pem', cert)
        pk_path = cnf.write_key('ec2-pk.pem', pk)
        cloud_cert_path = cnf.write_key('ec2-cloud-cert.pem', platform.get_ec2_cert())

        self.environ.update({
            'EC2_CERT': cert_path,
            'EC2_PRIVATE_KEY': pk_path,
            'EC2_USER_ID': platform.get_account_id(),
            'AWS_ACCESS_KEY': access_key,
            'AWS_SECRET_KEY': secret_key})
            # 'EC2_URL': platform.get_access_data('ec2_url')})
        self.credentials = {
            'cert': cert_path,
            'key': pk_path,
            'user': self.environ['EC2_USER_ID'],
            'access_key': access_key,
            'secret_key': secret_key}

    def _get_s3_bucket_name(self):
        platform = __node__['platform']
        return 'scalr2-images-%s-%s' % \
            (platform.get_region(), platform.get_account_id())

    def prepare(self, operation, role_name):
        pass
        
    def snapshot(self, operation, role_name):
        image_name = role_name + "-" + time.strftime("%Y%m%d%H%M%S")

        root_device_type = self._get_root_device_type()          
        root_disk = self._get_root_disk()
        self._setup_environment()
        LOG.debug('device type: %s' % root_device_type)
        if root_device_type == 'ebs':
            self.image_maker = EBSImageMaker(
                    image_name,
                    root_disk,
                    self)
        else:
            self.image_maker = InstanceStoreImageMaker(
                image_name,
                int(root_disk.size/1024),
                self,
                bucket_name=self._get_s3_bucket_name())

        # system2(('/usr/local/rvm/bin/rvm use 1.9.3',), shell=True)
        image_id = self.image_maker.create_image()
        # system2(('/usr/local/rvm/bin/rvm use system',), shell=True)
        return image_id

    def finalize(self, operation, role_name):
        pass

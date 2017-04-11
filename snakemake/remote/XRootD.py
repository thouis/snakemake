__author__ = "Chris Burr"
__copyright__ = "Copyright 2017, Chris Burr"
__email__ = "christopher.burr@cern.ch"
__license__ = "MIT"

import os
from os.path import abspath, join, normpath
import re

from snakemake.remote import AbstractRemoteObject, AbstractRemoteProvider
from snakemake.exceptions import WorkflowError, XRootDFileException

try:
    from XRootD import client
    from XRootD.client.flags import DirListFlags, MkDirFlags, StatInfoFlags
except ImportError as e:
    raise WorkflowError(
        "The Python 3 package 'XRootD' must be installed to use XRootD "
        "remote() file functionality. %s" % e.msg
    )


def parse_url(url):
    match = re.search('(?P<domain>(?:root://)?[A-Za-z0-9:\_\-\.]+\:?/)(?P<path>/.+)', url)
    if match is None:
        return None

    domain = match.group('domain')
    # Check if the protocol has been removed
    if not domain.startswith('root://'):
        domain = 'root://'+domain

    dirname, filename = os.path.split(match.group('path'))
    # We need a trailing / to keep XRootD happy
    dirname += '/'
    return domain, dirname, filename


class RemoteProvider(AbstractRemoteProvider):
    def __init__(self, *args, use_remote=False, **kwargs):
        super(RemoteProvider, self).__init__(*args, use_remote=use_remote, **kwargs)

        self._xrd = XRootDHelper(*args, **kwargs)

    def remote_interface(self):
        return self._xrd

    def remote(self, value, *args, use_remote=None, **kwargs):
        if use_remote is None:
            use_remote = self.use_remote

        def _strip_url(url):
            if not url.startswith('root://') and parse_url(url):
                raise XRootDFileException('Invalid xrootd url: '+url)
            domain, dirname, filename = parse_url(url)
            # Strip the prefix if we're not using the remote file
            to_strip = '' if self.use_remote else 'root://'
            return domain[len(to_strip):] + dirname + filename

        if isinstance(value, str):
            value = _strip_url(value)
        else:
            value = [_strip_url(v) for v in value]
        return super(RemoteProvider, self).remote(*args, value, *args, **kwargs)


class RemoteObject(AbstractRemoteObject):
    """ This is a class to interact with XRootD servers.
    """

    def __init__(self, *args, keep_local=False, use_remote=False, provider=None, **kwargs):
        super(RemoteObject, self).__init__(*args, keep_local=keep_local, use_remote=use_remote, provider=provider, **kwargs)

        if provider:
            self._xrd = provider.remote_interface()
        else:
            self._xrd = XRootDHelper(*args, **kwargs)

    def remote_file(self):
        domain, dirname, filename = parse_url(self.file())
        return "/"+self.file() if not self.file().startswith("/") else self.file()

    # === Implementations of abstract class members ===

    def exists(self):
        return self._xrd.exists(self.file())

    def mtime(self):
        if self.exists():
            return self._xrd.file_last_modified(self.file())
        else:
            raise XRootDFileException("The file does not seem to exist remotely: %s" % self.file())

    def size(self):
        if self.exists():
            return self._xrd.file_size(self.file())
        else:
            return self._iofile.size_local

    def download(self):
        self._xrd.copy(self.remote_file(), self.file())

    def upload(self):
        self._xrd.copy(self.file(), self.remote_file())

    @property
    def name(self):
        return self.file()

    @property
    def list(self):
        dirname = os.path.dirname(self._iofile.constant_prefix())+'/'
        files = list(self._xrd.list_directory_recursive(dirname))
        # Strip the prefix if we're not using the remote file
        to_strip = '' if self.use_remote else 'root://'
        return [normpath(f[len(to_strip):]) for f in files]

    def remove(self):
        self._xrd.remove(self.remote_file())


class XRootDHelper(object):

    def __init__(self):
        self._clients = {}

    def get_client(self, domain):
        try:
            return self._clients[domain]
        except KeyError:
            self._clients[domain] = client.FileSystem(domain)
            return self._clients[domain]

    def exists(self, url):
        domain, dirname, filename = parse_url(url)
        status, dirlist = self.get_client(domain).dirlist(dirname)
        if not status.ok:
            if status.errno == 3011:
                return False
            else:
                raise XRootDFileException(
                    'Error listing directory '+dirname+' on domain '+domain+
                    '\n'+repr(status)+'\n'+repr(dirlist))
        return filename in [f.name for f in dirlist.dirlist]

    def _get_statinfo(self, url):
        domain, dirname, filename = parse_url(url)
        matches = [f for f in self.list_directory(domain, dirname) if f.name == filename]
        assert len(matches) == 1
        return matches[0].statinfo

    def file_last_modified(self, filename):
        return self._get_statinfo(filename).modtime

    def file_size(self, filename):
        return self._get_statinfo(filename).size

    def copy(self, source, destination):
        # Prepare the source path for XRootD
        if not parse_url(source):
            source = abspath(source)
        # Prepare the destination path for XRootD
        if parse_url(destination):
            domain, dirname, filename = parse_url(destination)
            self.makedirs(domain, dirname)
        else:
            destination = abspath(destination)
        # Perform the copy operation
        process = client.CopyProcess()
        process.add_job(source, destination)
        process.prepare()
        status, returns = process.run()
        if not status.ok or not returns[0]['status'].ok:
            raise XRootDFileException('Error copying from '+source+' to '+destination, repr(status), repr(returns))

    def makedirs(self, domain, dirname):
        assert dirname.endswith('/')
        status, _ = self.get_client(domain).mkdir(dirname, MkDirFlags.MAKEPATH)
        if not status.ok:
            raise XRootDFileException('Failed to create directory '+dirname, repr(status))

    def list_directory(self, domain, dirname):
        status, dirlist = self.get_client(domain).dirlist(dirname, DirListFlags.STAT)
        if not status.ok:
            raise XRootDFileException(
                'Error listing directory '+dirname+' on domain '+domain+
                '\n'+repr(status)+'\n'+repr(dirlist)
            )
        return dirlist.dirlist

    def list_directory_recursive(self, start_url):
        assert start_url.endswith('/')
        domain, dirname, filename = parse_url(start_url)
        assert not filename
        filename = join(dirname, filename)
        for f in self.list_directory(domain, dirname):
            if f.statinfo.flags & StatInfoFlags.IS_DIR:
                for _f_name in self.list_directory_recursive(self, domain+dirname+f.name+'/'):
                    yield _f_name
            else:
                # Only yield files as directories don't have timestamps on XRootD
                yield domain+dirname+f.name

    def remove(self, url):
        domain, dirname, filename = parse_url(url)
        filename = join(dirname, filename)
        status, _ = self.get_client(domain).rm(filename)
        if not status.ok:
            raise XRootDFileException(
                'Failed to remove file '+filename+' from remote '+domain+'\n'+repr(status))
#!/usr/bin/python
"""
Expose a CloudFileFS object over SFTP using paramiko

Copyright (C) 2011 by Memset Ltd. http://www.memset.com/

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

"""

import logging

import os
import stat as statinfo
import time
from SocketServer import StreamRequestHandler, ThreadingTCPServer
import threading

import paramiko

from ftpcloudfs.fs import CloudFilesFS
from StringIO import StringIO
from functools import wraps

from posixpath import basename #FIXME put in cloudfilesfs?
from sftpcloudfs.constants import version

def return_sftp_errors(func):
    """
    Decorator to catch EnvironmentError~s and return SFTP error codes instead.

    Other exceptions are not caught.
    """
    @wraps(func)
    def wrapper(*args,**kwargs):
        name = getattr(func, "func_name", "unknown")
        try:
            logging.debug("%s(%r,%r): enter" % (name, args, kwargs))
            rc = func(*args,**kwargs)
        except EnvironmentError, e:
            logging.debug("%s: caught error: %s" % (name, e))
            rc = paramiko.SFTPServer.convert_errno(e.errno)
        logging.debug("%s: returns %r" % (name, rc))
        return rc
    return wrapper


class SFTPServerInterface(paramiko.SFTPServerInterface):
    """
    SFTPServerInterface implementation that exposes a CloudFilesFS object
    """

    def __init__(self, server, fs, *args, **kwargs):
        self.fs = fs
        self.fs.flush()
        self.log = paramiko.util.get_logger("paramiko.transport")
        self.log.debug("%s: start filesystem interface" % self.__class__.__name__)
        super(SFTPServerInterface,self).__init__(server, *args, **kwargs)

    @return_sftp_errors
    def open(self, path, flags, attr):
        return SFTPHandle(self, path, flags)

    @return_sftp_errors
    def list_folder(self, path):
        return [ paramiko.SFTPAttributes.from_stat(stat, leaf)
                 for leaf, stat in self.fs.listdir_with_stat(path) ]

    @return_sftp_errors
    def stat(self, path):
        stat = self.fs.stat(path)
        filename = basename(path)
        return paramiko.SFTPAttributes.from_stat(stat, filename)

    def lstat(self, path):
        return self.stat(path)

    @return_sftp_errors
    def remove(self, path):
        self.fs.remove(path)
        return paramiko.SFTP_OK

    @return_sftp_errors
    def rename(self, oldpath, newpath):
        self.fs.rename(oldpath, newpath)
        return paramiko.SFTP_OK

    @return_sftp_errors
    def mkdir(self, path, attr):
        self.fs.mkdir(path)
        return paramiko.SFTP_OK

    @return_sftp_errors
    def rmdir(self, path):
        self.fs.rmdir(path)
        return paramiko.SFTP_OK

    def canonicalize(self, path):
        return self.fs.abspath(self.fs.normpath(path))

    @return_sftp_errors
    def chattr(self, path, attr):
        return paramiko.SFTP_OP_UNSUPPORTED

    def readlink(self, path):
        return paramiko.SFTP_OP_UNSUPPORTED

    def symlink(self, path):
        return paramiko.SFTP_OP_UNSUPPORTED


class SFTPHandle(paramiko.SFTPHandle):
    """
    Expose a CloudFilesFD object to SFTP
    """

    def __init__(self, owner, path, flags):
        super(SFTPHandle, self).__init__(flags)
        self.log = paramiko.util.get_logger("paramiko.transport")
        self.owner = owner
        self.path = path
        self.log.debug("SFTPHandle(path=%r, flags=%r)" % (path, flags))
        open_mode = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        if open_mode == os.O_RDONLY:
            mode = "r"
        elif open_mode == os.O_WRONLY:
            mode = "w"
        elif open_mode == os.O_RDWR:
            mode = "rw"
        else:
            self.log.error("Bad open mode %r" % flags)
            return parmiko.SFTP_OP_UNSUPPORTED
        if flags & os.O_APPEND:
            mode += "+"
        # FIXME ignores os.O_CREAT, os.O_TRUNC, os.O_EXCL
        self._file = owner.fs.open(path, mode)
        self._tell = 0

    @return_sftp_errors
    def close(self):
        self._file.close()
        return paramiko.SFTP_OK

    @return_sftp_errors
    def read(self, offset, length):
        if offset != self._tell:
            return paramiko.SFTP_OP_UNSUPPORTED
            # FIXME self._file.seek(offset)
        data = self._file.read(length)
        self._tell += len(data)
        return data

    @return_sftp_errors
    def write(self, offset, data):
        if offset != self._tell:
            return paramiko.SFTP_OP_UNSUPPORTED
            # FIXME self._file.seek(offset)
        self._file.write(data)
        self._tell += len(data)
        return paramiko.SFTP_OK

    def stat(self):
        return self.owner.stat(self.path)

    def chattr(self,attr):
        return SFTP_OP_UNSUPPORTED


class CloudFilesSFTPRequestHandler(StreamRequestHandler):
    """
    SocketServer RequestHandler subclass for CloudFilesSFTPServer.

    This RequestHandler subclass creates a paramiko Transport, sets up the
    sftp subsystem, and hands off to the transport's own request handling
    thread.  Note that paramiko.Transport uses a separate thread by default,
    so there is no need to use ThreadingMixin.
    """

    timeout = 60
    auth_timeout = 60

    def handle(self):
        self.log = paramiko.util.get_logger("paramiko.transport")
        self.log.debug("%s: start transport" % self.__class__.__name__)
        t = paramiko.Transport(self.request)
        t.add_server_key(self.server.host_key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, SFTPServerInterface, self.server.fs)
        t.start_server(server=self.server)
        chan = t.accept(self.auth_timeout)
        if chan is None:
            self.log.warning("Channel is None, closing")
            t.close()
            return
        t.join()

class CloudFilesSFTPServer(ThreadingTCPServer, paramiko.ServerInterface):
    """
    Expose a CloudFilesFS object over SFTP
    """
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, host_key=None, authurl=None):
        self.log = paramiko.util.get_logger("paramiko.transport")
        self.log.debug("%s: start server" % self.__class__.__name__)
        self.fs = CloudFilesFS(None, None, authurl=authurl) # unauthorized
        self.host_key = host_key
        ThreadingTCPServer.__init__(self, address, CloudFilesSFTPRequestHandler)

    def close_request(self, request):
        # do nothing paramiko.Transport deals with it
        pass

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        self.log.warning("Channel request denied, kind=%s" % kind)
        # all the check_channel_*_request return False by default but
        # sftp subsystem because of the set_subsystem_handler call in
        # the CloudFilesSFTPRequestHandler
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_none(self, username):
        """Check whether the user can proceed without authentication."""
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        """Check whether the given public key is valid for authentication."""
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        """Check whether the given password is valid for authentication."""
        self.log.info("Auth request (type=password), username=%s" % username)

        if not password:
            self.log.error("Failed to authenticate: no password provided")
            return paramiko.AUTH_FAILED

        try:
            self.fs.authenticate(username, password)
        except EnvironmentError, e:
            self.log.error("Failed to authenticate: %s" % e)
            return paramiko.AUTH_FAILED
        self.log.debug("%s: authenticated" % self.__class__.__name__)
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self,username):
        """Return string containing a comma separated list of allowed auth modes.

        The available modes are  "node", "password" and "publickey".
        """
        return "password"


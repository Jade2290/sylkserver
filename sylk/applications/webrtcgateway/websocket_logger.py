# Copyright (C) 2015 AG Projects. See LICENSE for details.
#

"""
Logging support for WebSocket traffic.
"""

__all__ = ["Logger"]

import os
import sys

from application.system import makedirs
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread


class Logger(object):
    def __init__(self):
        self.stopped = False
        self._wstrace_filename = None
        self._wstrace_file = None
        self._wstrace_error = False
        self._wstrace_start_time = None
        self._wstrace_packet_count = 0

        self._log_directory_error = False

    def start(self):
        # try to create the log directory
        try:
            self._init_log_directory()
            self._init_log_file()
        except Exception:
            pass
        self.stopped = False

    def stop(self):
        self.stopped = True
        if self._wstrace_file is not None:
            self._wstrace_file.close()
            self._wstrace_file = None

    def msg(self, direction, timestamp, packet):
        if self._wstrace_start_time is None:
            self._wstrace_start_time = timestamp
        self._wstrace_packet_count += 1
        buf = ["%s: Packet %d, +%s" % (direction, self._wstrace_packet_count, (timestamp - self._wstrace_start_time))]
        buf.append(packet)
        buf.append('--')
        message = '\n'.join(buf)
        self._process_log((message, timestamp))

    @run_in_thread('log-io')
    def _process_log(self, record):
        if self.stopped:
            return
        message, timestamp = record
        try:
            self._init_log_file()
        except Exception:
            pass
        else:
            self._wstrace_file.write('%s [%s %d]: %s\n' % (timestamp, os.path.basename(sys.argv[0]).rstrip('.py'), os.getpid(), message))
            self._wstrace_file.flush()

    def _init_log_directory(self):
        settings = SIPSimpleSettings()
        log_directory = settings.logs.directory.normalized
        try:
            makedirs(log_directory)
        except Exception, e:
            if not self._log_directory_error:
                print "failed to create logs directory '%s': %s" % (log_directory, e)
                self._log_directory_error = True
            self._wstrace_error = True
            raise
        else:
            self._log_directory_error = False
            if self._wstrace_filename is None:
                self._wstrace_filename = os.path.join(log_directory, 'webrtcgateway_trace.log')
                self._wstrace_error = False

    def _init_log_file(self):
        if self._wstrace_file is None:
            self._init_log_directory()
            filename = self._wstrace_filename
            try:
                self._wstrace_file = open(filename, 'a')
            except Exception, e:
                if not self._wstrace_error:
                    print "failed to create log file '%s': %s" % (filename, e)
                    self._wstrace_error = True
                raise
            else:
                self._wstrace_error = False


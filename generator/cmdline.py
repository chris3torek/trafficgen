import errno
import os
import sys
import pprint
import cStringIO
import tempfile
import time
import threading

import bess
import cli
from module import *

import commands as bess_commands
import generator_commands
from common import *

class TGENCLI(cli.CLI):
    def __init__(self, bess, cmd_db, **kwargs):
        self.bess = bess
        self.bess_lock = threading.Lock()
        self.cmd_db = cmd_db
        self.__running = dict() 
        self.__running_lock = threading.Lock()
        self.__monitor_thread = None
        self.__done = False
        self.__done_lock = threading.Lock()
        self.this_dir = bess_path = os.getenv('BESS_PATH') + '/bessctl'

        super(TGENCLI, self).__init__(self.cmd_db.cmdlist, **kwargs)

    def port_is_running(self, port):
        with self.__running_lock:
            ret = port in self.__running
        return ret

    def add_session(self, sess):
        with self.__running_lock:
            self.__running[str(sess.port())] = sess

    def remove_session(self, port):
        with self.__running_lock:
            ret = self.__running.pop(port, None)
        return ret

    def get_session(self, port):
        with self.__running_lock:
            ret = self.__running.get(str(port), None)
        return ret

    def clear_sessions(self):
        with self.__running_lock:
            self.__running.clear()

    def _done(self):
        with self.__done_lock:
            ret = self.__done
        return ret

    def _finish(self):
        with self.__done_lock:
            self.__done = True
        self.__monitor_thread.join()

    def monitor_thread(self):
        while not self._done():
            now = time.time()
            with self.__running_lock:
                try:
                    with self.bess_lock:
                        for port, sess in self.__running.items():
                            if sess.spec().latency:
                                sess.update_rtt(self)
                            else:
                                sess.update_port_stats(self, now)

                        self.bess.pause_all()
                        try:
                            for port, sess in self.__running.items():
                                if not sess.spec().latency:
                                    sess.adjust_tx_rate(self)
                        finally:
                            self.bess.resume_all()
                except bess.BESS.APIError:
                    pass
                except:
                    raise
            sleep_us(ADJUST_WINDOW_US)
        print('Port monitor thread exiting...')

    def get_var_attrs(self, var_token, partial_word):
        return self.cmd_db.get_var_attrs(self, var_token, partial_word)

    def split_var(self, var_type, line):
        try:
            return self.cmd_db.split_var(self, var_type, line)
        except self.InternalError:
            return super(TGENCLI, self).split_var(var_type, line)

    def bind_var(self, var_type, line):
        try:
            return self.cmd_db.bind_var(self, var_type, line)
        except self.InternalError:
            return super(TGENCLI, self).bind_var(var_type, line)

    def print_banner(self):
        self.fout.write('Type "help" for more information.\n')

    def get_default_args(self):
        return [self]

    def _handle_broken_connection(self):
        host = self.bess.peer[0]
        if host == 'localhost' or self.bess.peer[0].startswith('127.'):
            self._print_crashlog()
        self.bess.disconnect()

    def call_func(self, func, args):
        try:
            super(TGENCLI, self).call_func(func, args)

        except self.bess.APIError as e:
            self.err(e)
            raise self.HandledError()

        except self.bess.RPCError as e:
            self.err('RPC failed to {}:{} - {}'.format(
                    self.bess.peer[0], self.bess.peer[1], e.message))

            self._handle_broken_connection()
            raise self.HandledError()

        except self.bess.Error as e:
            self.err(e.errmsg)

            if e.err in errno.errorcode:
                err_code = errno.errorcode[e.err]
            else:
                err_code = '<unknown>'

            self.ferr.write('  BESS daemon response - errno=%d (%s: %s)\n' %
                            (e.err, err_code, os.strerror(e.err)))

            if e.details:
                details = pprint.pformat(e.details)
                initial_indent = '  error details: '
                subsequent_indent = ' ' * len(initial_indent)

                for i, line in enumerate(details.splitlines()):
                    if i == 0:
                        self.fout.write('%s%s\n' % (initial_indent, line))
                    else:
                        self.fout.write('%s%s\n' % (subsequent_indent, line))

            raise self.HandledError()

    def _print_crashlog(self):
        try:
            log_path = tempfile.gettempdir() + '/bessd_crash.log'
            log = open(log_path).read()
            ctime = time.ctime(os.path.getmtime(log_path))
            self.ferr.write('From {} ({}):\n{}'.format(log_path, ctime, log))
        except Exception as e:
            self.ferr.write('%s is not available: %s' % (log_path, str(e)))

    def loop(self):
        print('Spawning port monitor thread...')
        self.__monitor_thread = threading.Thread(target=self.monitor_thread)
        self.__monitor_thread.start()
        try:
            super(TGENCLI, self).loop()
        finally:
            self._finish()
        print('Killing BESS...')
        bess_commands._do_stop(self)

    def get_prompt(self):
        if self.bess.is_connected():
            return '%s:%d $ ' % self.bess.peer

        if self.bess.is_connection_broken():
            self._handle_broken_connection()

        return '<disconnected> $ '


class ColorizedOutput(object):
    def __init__(self, orig_out, color):
        self.orig_out = orig_out
        self.color = color

    def __getattr__(self, attr):
        def_color = '\033[0;0m'  # resets all terminal attributes

        if attr == 'write':
            return lambda x: self.orig_out.write(self.color + x + def_color)
        else:
            return getattr(self.orig_out, attr)


def run_cli():
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    # Colorize output to standard error
    if interactive and sys.stderr.isatty():
        stderr = ColorizedOutput(sys.stderr, '\033[31m')  # red (not bright)
    else:
        stderr = sys.stderr

    try:
        hist_file = os.path.expanduser('~/.trafficgen_history')
        open(hist_file, 'a+').close()
    except:
        print >> stderr, 'Error: Cannot open ~/.trafficgen_history'
        hist_file = None
        raise

    try:
        s = bess.BESS()
        s.connect()
    except bess.BESS.APIError as e:
        print >> stderr, e.message, '(bessd daemon is not running?)'

    cli = TGENCLI(s, generator_commands, ferr=stderr, interactive=interactive,
                  history_file=hist_file)
    print('Starting BESS...')
    bess_commands._do_start(cli, '')
    bess_commands.warn(cli, 'About to clear any existing BESS pipelines.',
        bess_commands._do_reset)
    cli.loop()

#!/usr/bin/env python3
import os
import zmq
import sys
import stat
import psutil
import argparse
import json
import platform
from datetime import datetime
from humetools import (
    NotImplementedAction, printerr, pprinterr, valueOrDefault, envOrDefault,
    is_valid_hostname
)


__version__ = '1.2.28'
# Version of the hume message format
MESSAGE_VERSION = 1


class Hume():
    # 'unknown' is added for Nagios compatibility
    LEVELS = ['info', 'ok', 'warning', 'error', 'critical', 'debug', 'unknown']
    DEFAULT_LEVEL = 'info'
    NO_TAGS = []
    NO_TASKID = ''
    RECVTIMEOUT = 1000

    def __init__(self, args):
        self.config = {'url': 'tcp://127.0.0.1:198'}

        # args
        self.args = args

        # extra
        if hasattr(args, 'extra'):
            if args.extra is None:
                self.extra = {}
            else:
                self.extra = self.dictify_extra_vars(args.extra)
        else:
            self.extra = {}

        self.verbose = valueOrDefault(args, 'verbose', False)
        self.token = valueOrDefault(args, "token", None)

        # Prepare object to send
        # Might end up moving some of this stuff around
        # But I like focusing blocks to be developed
        # in such a way that the code can grow organically
        # and be coder-assistive
        self.reqObj = {}
        if self.token:
            self.reqObj["token"] = self.token
        # To store information related to how hume was executed
        self.reqObj['process'] = {}
        # Hume-specific information
        self.reqObj['hume'] = {}
        # Mandatory
        self.reqObj['hume']['timestamp'] = self.get_timestamp()
        self.reqObj['hume']['version'] = MESSAGE_VERSION
        # Stores hume-client hostname. Should not be confused with the
        # humePkt-level hostname that humed stores. In some weird
        # instances hume and humed might be running in different machines.
        self.reqObj['hume']['hostname'] = valueOrDefault(args,
                                                 'hostname',
                                                 platform.node())
        # Make sure to set a default value
        self.reqObj['hume']['level'] = valueOrDefault(args,
                                                      'level',
                                                      Hume.DEFAULT_LEVEL)
        self.reqObj['hume']['tags'] = valueOrDefault(args,
                                                     'tags',
                                                     Hume.NO_TAGS)
        self.reqObj['hume']['task'] = valueOrDefault(args,
                                                     'task',
                                                     Hume.NO_TASKID)
        self.reqObj['hume']['msg'] = valueOrDefault(args, 'msg', '')

        # The extra field in hume is used to store additional
        # information, and is not subject to hume design parameters.
        self.reqObj['hume']['extra'] = self.extra
        # Very optional ones:
        try:
            if self.args.append_pstree or 'append_pstree' in self.args.keys():
                self.reqObj['process']['tree'] = self.get_pstree()
        except AttributeError:
            pass

        ln = self.get_lineno()
        if ln is not None:
            self.reqObj['process']['line_number'] = ln
        del ln

        if (len(self.reqObj['process']) == 0):
            del(self.reqObj['process'])

        if self.config['url'].startswith('ipc://'):
            if not self.test_unix_socket(config['url']):
                print('socket not writable or other error')
                sys.exit(1)

    def dictify_extra_vars(self, extra_vars):
        if extra_vars is None:
            return({})
        d = {}
        for item in extra_vars:
            for c in [':', '=']:  # Yeah, I know...
                if item.count(c) == 1:
                    splitChar = c
            (var, val) = item.split(splitChar)
            d[var] = val
        return(d)

    def test_unix_socket(self, url):
        path = url.replace('ipc://', '')
        if not os.path.exists(path):
            return(False)
        mode = os.stat(path).st_mode
        isSocket = stat.S_ISSOCK(mode)
        if not isSocket:
            return(False)
        if os.access(path, os.W_OK):
            # OK, it's an actual socket we can write to
            return(True)
        return(False)

    def send(self, encrypt_to=None):
        # TODO: If we were to encrypt, we would encapsulate
        # self.reqObj to a special structure:
        # {'payload': ENCRYPTED_ASCII_ARMORED_CONTENT,
        #  'encrypted': True}
        # or something like that. Also, probably kant would
        # be useful in this context...
        if encrypt_to is None:
            HUME = self.reqObj
        else:
            HUME = self.encrypt(gpg_encrypt_to)
        # The abstraction level of zeromq does not allow to
        # simply check for correctly sent messages. We should wait for a REPly
        # FIX: see if we can make REP/REQ work as required
        sock = zmq.Context().socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        if self.verbose:
            printerr('Hume: connecting to {}'.format(self.config['url']))
        try:
            sock.connect(self.config['url'])
        except zmq.ZMQError as exc:
            print(exc)
            sys.exit(2)
        if self.verbose:
            printerr('Hume: Sending hume...')
        try:
            x = sock.send_string(json.dumps(self.reqObj))
        except zmq.ZMQError as exc:
            msg = "\033[1;33mEXCEPTION:\033[0;37m{}"
            print(msg.format(exc))
            sys.exit(3)
        except Exception as exc:
            print("Unknown exception: {}".format(exc))
            sys.exit(4)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        if poller.poll(valueOrDefault(self.args,
                                      'recvtimeout',
                                      Hume.RECVTIMEOUT)):
            if self.verbose:
                printerr('Hume: response received within timeout')
            msg = sock.recv_string().strip()
        else:
            if self.verbose:
                printerr('Timeout sending hume')
            sock.close()
            return
        sock.close()
        # TODO: validate OK vs other errors. needs protocol def.
        if msg == 'OK':
            return(True)
        return(False)

    def get_pstree(self):  # FIX: make better version
        ps_tree = []
        h = 0
        me = psutil.Process()
        parent = psutil.Process(me.ppid())
        while parent.ppid() != 0:
            ps_tree.append({'pid': parent.pid,
                            'cmdline': parent.cmdline(),
                            'order': h})
            parent = psutil.Process(parent.ppid())
            h = h+1
        return(ps_tree)

    def get_caller(self):
        me = psutil.Process()
        parent = psutil.Process(me.ppid())
        grandparent = psutil.Process(parent.ppid())
        return(grandparent.cmdline())

    def get_lineno(self):
        try:
            return(os.environ['LINENO'])
        except Exception:
            # TODO: add stderr warning about no LINENO
            return(None)

    def get_timestamp(self):
        return(datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f'))


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version',
                        action='version',
                        version='HumeClient v{} by Buanzo'.format(__version__))
    parser.add_argument('--verbose',
                        action='store_true',
                        dest='verbose')
    parser.add_argument("-L", "--level",
                        type=str.lower,  # TODO: also check when instancing
                        choices=Hume.LEVELS,
                        default=Hume.DEFAULT_LEVEL,
                        help="Level of update to send, defaults to 'info'")
    parser.add_argument("-c", "--hume-cmd",
                        choices=['counter-start',
                                 'counter-pause',
                                 'counter-stop',
                                 'counter-reset'],
                        default='',
                        dest='humecmd',
                        required=False,
                        help="[OPTIONAL] Command to attach to the update.")
    parser.add_argument("-t", "--task",
                        required=False,
                        default=envOrDefault('HUME_TASKNAME', ''),
                        help='''[OPTIONAL] Task name, for example BACKUPTASK.
Takes precedente over HUME_TASKNAME envvar.''')
    parser.add_argument('-a', '--append-pstree',
                        action='store_true',
                        help="Append process calling tree")
    parser.add_argument('-T', '--tags',
                        action='append',
                        default=[envOrDefault('HUME_TAGS', '')],
                        help='''Comma-separated list of tags. HUME_TAGS
envvar contents are appended.''')
    parser.add_argument('-e', '--encrypt-to',
                        default=None,
                        action=NotImplementedAction,
                        dest='encrypt_to',
                        help="[OPTIONAL] Encrypt to this gpg pubkey id")
    parser.add_argument('--auth-token',
                        default=envOrDefault('HUME_TOKEN', ''),
                        dest='token',
                        help='Authentication token for humed. Defaults to HUME_TOKEN envvar')
    parser.add_argument('--recv-timeout',
                        default=int(envOrDefault('HUME_RECVTIMEOUT', 1000)),
                        type=int,
                        dest='recvtimeout',
                        help='''Time to wait for humed reply to hume message.
Default 1000ms / 1 second. Takes precedence over HUME_RECVTIMEOUT envvar.''')
    parser.add_argument('--hostname',
                        default=platform.node(),
                        dest='hostname',
                        help='''[OPTIONAL] Sets hostname to use in hume
message. Defaults to detected hostname "{}"'''.format(platform.node()))
    parser.add_argument('-x', '--extra',
                        action='append',
                        dest='extra',
                        metavar='VAR=VALUE or VAR:VALUE',
                        help='''Sends an additional variable=value with the
hume message. Can be used multiple times.
Example: -x identifier=abc1 -x age=42''')
    parser.add_argument('msg',
                        help="[REQUIRED] Message to include with this update")
    args = parser.parse_args()

    if not is_valid_hostname(args.hostname):
        printerr('Hostname is not valid. Ignoring hume.')
        sys.exit(1)

    # Allows for multiple --tags tag1,tag2 --tags tag3,tag4 to be a simple list
    fulltags = []
    if args.tags is not None:
        for item in args.tags:
            if len(item) > 0:
                fulltags.extend(item.split(','))
        args.tags = fulltags
    else:
        args.tags = []

    # Now we call the send method while initializing Hume() directly
    # with the parsed args.
    if args.verbose:
        print('About to send hume...')
    r = Hume(args).send(encrypt_to=args.encrypt_to)
    if args.verbose:
        print('Back from huming.')
    if r is True:
        sys.exit(0)
    sys.exit(1)


if __name__ == '__main__':
    run()
    sys.exit(0)

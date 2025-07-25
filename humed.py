#!/usr/bin/python3
import logging
from logging import getLogger
from hume import __version__, MESSAGE_VERSION, Hume
import sys
import zmq
import json
import socket
import sqlite3
import datetime
import argparse
import socket
import requests
import platform
from logging.handlers import SysLogHandler
from pid.decorator import pidfile
from queue import Queue
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from functools import partial
import time
from humetools import printerr, pprinterr, is_valid_hostname, HumeRenderer
from humed_plugins import load_plugins, get_plugin, plugin_config_templates
# The Confuse library is awesome.
import confuse

DEVMODE = False

# Supported message format versions
SUPPORTED_MSG_VERSIONS = [MESSAGE_VERSION]

# Basic list of TRANSFER_METHODS
# We extend TRANSFER_METHODS by testing for optional modules
# TODO: kant will be our own
TRANSFER_METHODS = ['syslog', 'rsyslog', 'slack', 'kant']

# availability test for logstash (see optional_requirements.txt)
try:
    from logstash_async.handler import AsynchronousLogstashHandler as AsyncLSH
except ImportError:
    # logstash not available
    pass
else:
    # You gotta love try/except/else/finally
    TRANSFER_METHODS.append('logstash')

# Load external plugins and extend list of transfer methods
PLUGINS = load_plugins()
for name in PLUGINS.keys():
    if name not in TRANSFER_METHODS:
        TRANSFER_METHODS.append(name)

# On Humed() __init__, we scan the templates dir and get
# base template names for each transfer_method.
# We save that in a dictionary.
# A default base template will ship with humed for each transfer_method.
# Currently, only slack is there.
# templates_dir = {config_dir}/templates/{TRANSFER_METHOD}/{BASE}[_level].tpl
#
# This way, if user wants to use a template base 'example' in the 'slack'
# transfer_method, then humed will apply the available template by level:
# For warning level message via slack using 'example':
# template = /etc/humed/templates/slack/example_warning.tpl
# If level-specific template is not available, it tries default.
# If default-level transfer method specific template is not available,
# it tries level-specific transfer default.
# If that's not there, it goes default_default.
# If THAT"s not there your humed is bonked but it fallbacks to old style.
#
# This way you can integrate different priority levels with custom
# coloring, action links, etc.
#
BASE_TEMPLATES = {}
for method in TRANSFER_METHODS:
    BASE_TEMPLATES[method] = []

# TODO: add determination for fluentd-logger, we still need to find a GOOD
# implementation

# Configuration template
# See:
# https://github.com/beetbox/confuse/blob/master/example/__init__.py
config_template = {  # TODO: add debug. check confuse.Bool()
    'endpoint': confuse.String(),
    'transfer_method': confuse.OneOf(TRANSFER_METHODS),
    'syslog': {
        'template_base': confuse.OneOf(BASE_TEMPLATES['syslog']),
    },
    'rsyslog': {
        'server': confuse.String(),
        'proto': confuse.OneOf(['tcp', 'udp']),
        'port': confuse.Integer(),
        'template_base': confuse.OneOf(BASE_TEMPLATES['rsyslog']),
    },
    'logstash': {
        'host': confuse.String(),
        'port': confuse.Integer(),
        'template_base': confuse.OneOf(BASE_TEMPLATES['logstash']),
    },
    'slack': {
        'webhook_default': confuse.String(),  # for ok and info messages
        'webhook_warning': confuse.String(),
        'webhook_error': confuse.String(),
        'webhook_critical': confuse.String(),
        'webhook_debug': confuse.String(),
        'template_base': confuse.OneOf(BASE_TEMPLATES['slack']),
    },
    'metrics': {
        'port': confuse.Integer(),
        'token': confuse.Optional(confuse.String()),
    },
    'auth_token': confuse.Optional(confuse.String()),
}

# Merge plugin-provided configuration templates
for _name, _tpl in plugin_config_templates.items():
    config_template[_name] = _tpl


class Humed():
    def __init__(self, config):
        # We will only expose config if needed
        # self.config = config
        self.debug = config['debug'].get()
        # Database path depends on debug
        self.dbpath = '/var/log/humed.sqlite3'
        if DEVMODE:
            self.dbpath = './humed.sqlite3'
        self.endpoint = config['endpoint'].get()
        self.transfer_method = config['transfer_method'].get()
        try:
            self.humed_hostname = config['hostname'].get()
        except Exception:
            self.humed_hostname = platform.node()
        self.plugin = get_plugin(self.transfer_method)
        if self.plugin:
            try:
                self.transfer_method_args = config[self.transfer_method].get()
            except Exception:
                self.transfer_method_args = {}
        else:
            self.transfer_method_args = config[self.transfer_method].get()
        try:
            self.metrics_port = config['metrics']['port'].get()
        except Exception:
            self.metrics_port = None
        try:
            self.metrics_token = config['metrics']['token'].get()
        except Exception:
            self.metrics_token = None
        try:
            self.auth_token = config['auth_token'].get()
        except Exception:
            self.auth_token = None
        self.status = {}
        self.metrics_server = None
        # Queue and Worker
        self.queue = Queue()
        worker = Thread(target=self.worker_process_transfers)
        worker.daemon = True
        worker.start()
        self.start_metrics_server()

        # HumeRenderer
        templates_dir = '{}/templates/{}'.format(config.config_dir(),
                                                self.transfer_method)
        if self.debug:
            printerr('Templates_dir = {}'.format(templates_dir))
        self.renderer = HumeRenderer(templates_dir=templates_dir,
                                     transfer_method=self.transfer_method,
                                     debug=self.debug)
        BASE_TEMPLATES[self.transfer_method] = self.renderer.available_bases()

        # TODO: improve, and support multi transfer methods, multi renders
        self.logger = getLogger('humed-{}'.format(self.transfer_method))
        self.logger.setLevel(logging.INFO)
        if self.transfer_method is 'logstash':
            host = self.transfer_method_args['host'].get()
            port = self.transfer_method_args['host'].get()
            self.logger.addHandler(AsyncLSH(host,
                                            port,
                                            database_path='logstash.db'))
        # We will replace this with a plugin-oriented approach ASAP
        elif self.transfer_method is 'rsyslog':
            server = self.config['rsyslog']['server'].get()
            port = self.config['rsyslog']['port'].get()
            proto = self.config['rsyslog']['proto'].get()
            sa = (server, port)
            if proto is 'udp':
                socktype = socket.SOCK_DGRAM
            elif proto is 'tcp':
                socktype = socket.SOCK_STREAM
            else:
                printerr('Unknown proto "{}" in __init__')
                sys.exit(127)
            self.logger.addHandler(SysLogHandler(address=sa,
                                                 socktype=socktype))
        elif self.transfer_method is 'syslog':
            self.logger.addHandler(logging.handlers.SysLogHandler())
        # no 'else' because confuse takes care of validating config options

        if self.prepare_db() is False:
            sys.exit('Humed: Error preparing database')

    def packet_upgrade_check(self, item):
        # TODO: We need to include hume pkt version. dammit.
        # I am SOOOOOOOOOOO rewriting hume..
        return(item)
        
    def worker_process_transfers(self):  # TODO
        while True:
            item = self.queue.get()
            if self.debug:
                pprinterr(item)
            pendientes = self.list_transfers2(pending=True)
            if self.debug:
                printerr('Pending Items to send: {}'.format(len(pendientes)))
                printerr('Methods: {}'.format(self.transfer_method))
            for rowid in pendientes:
                humepkt = self.get_humepkt_from_transfers(rowid=rowid)
                #item = packet_upgrade_check(item)
                if self.transfer_method == 'logstash':
                    ret = self.logstash(item=item)
                elif self.transfer_method == 'syslog':
                    ret = self.syslog(item=item)  # using std SysLogHandler
                elif self.transfer_method == 'rsyslog':
                    ret = self.syslog(item=item)  # using std SysLogHandler
                elif self.transfer_method == 'slack':
                    ret = self.slack(humepkt=humepkt, rowid=rowid)
                elif self.plugin:
                    try:
                        ret = self.plugin.send(humepkt=humepkt, config=self.transfer_method_args)
                    except Exception as exc:
                        if self.debug:
                            printerr(f'Plugin {self.transfer_method} failed: {exc}')
                        ret = False
                if ret is True:
                    self.transfer_ok(rowid=rowid)
            self.queue.task_done()

    def get_sqlite_conn(self):
        try:
            conn = sqlite3.connect(self.dbpath)
        except Exception as ex:
            printerr(ex)
            printerr('Error connecting to sqlite3 on "{}"'.format(self.dbpath))
            return(None)
        return(conn)

    def prepare_db(self):
        try:
            self.conn = sqlite3.connect(self.dbpath)
        except Exception as ex:
            printerr(ex)
            printerr('Humed: cant connect sqlite3 on "{}"'.format(self.dbpath))
        self.cursor = self.conn.cursor()
        try:
            sql = '''CREATE TABLE IF NOT EXISTS
                     transfers (ts timestamp, sent boolean, hume text)'''
            self.cursor.execute(sql)
            self.conn.commit()
        except Exception as ex:
            printerr(ex)
            return(False)
        return(True)

    def transfer_ok(self, rowid):  # add a DELETE somewhere sometime :P
        try:
            sql = 'UPDATE transfers SET sent=1 WHERE rowid=?'
            conn = self.get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute(sql, (rowid,))
            conn.commit()
        except Exception as ex:
            printerr(ex)
            return(False)
        return(True)

    def add_transfer(self, hume):
        try:
            hume = json.dumps(hume)
        except Exception as ex:
            printerr('Humed - add_transfer() json dumps exception:')
            printerr(ex)
            return(None)  # FIX: should we exit?
        try:
            now = datetime.datetime.now()
            sql = 'INSERT INTO transfers(ts, sent, hume) VALUES (?,?,?)'
            self.cursor.execute(sql, (now, 0, hume,))
            self.conn.commit()
        except Exception as ex:
            printerr('Humed: add_transfer() Exception:')
            printerr(ex)
            return(None)
        return(self.cursor.lastrowid)

    def list_transfers(self, pending=False):
        if pending is True:
            sql = 'SELECT rowid,* FROM transfers WHERE sent = 0'
        else:
            sql = 'SELECT rowid,* FROM transfers'
        lista = []
        rows = []
        try:
            conn = self.get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
        except Exception as ex:
            printerr(ex)

        for row in rows:
            lista.append(row)
        return(lista)

    def get_humepkt_from_transfers(self, rowid=None):
        if rowid is None:
            return(None)
        sql = 'SELECT * FROM transfers WHERE rowid = ?'
        try:
            conn = self.get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute(sql, (rowid,))
            rows = cursor.fetchall()
        except Exception as ex:
            printerr(ex)
        ts = rows[0][0]
        try:
            hume = json.loads(rows[0][2])
        except Exception as ex:
            if self.debug:
                printerr("Malformed json packet ROWID#{}.".format(rowid))
            return(False)  # FIX: malformed json at this stage? mmm
        if 'extra' not in hume['hume'].keys():
            hume['hume']['extra'] = {}
        else:
            # might be there but be invalid
            if hume['hume']['extra'] is None:
                hume['hume']['extra'] = {}
        humepkt={}
        humepkt['rowid'] = rowid
        humepkt['ts'] = ts
        humepkt['hume'] = hume
        return(humepkt)
        
    def list_transfers2(self, pending=False):
        if pending is True:
            sql = 'SELECT rowid FROM transfers WHERE sent = 0'
        else:
            sql = 'SELECT rowid FROM transfers'
        lista = []
        rows = []
        try:
            conn = self.get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
        except Exception as ex:
            printerr(ex)

        for row in rows:
            lista.append(row[0])
        return(lista)

    def process_transfers(self):
        pendientes = self.list_transfers2(pending=True)
        if self.debug:
            printerr('Pending Items to send: {}'.format(len(pendientes)))
            printerr('Methods: {}'.format(self.transfer_method))
        for rowid in pendientes:
            humepkt = self.get_humepkt_from_transfers(rowid=rowid)
            if self.transfer_method == 'logstash':
                ret = self.logstash(item=item)
            elif self.transfer_method == 'syslog':
                ret = self.syslog(item=item)  # using std SysLogHandler
            elif self.transfer_method == 'rsyslog':
                ret = self.syslog(item=item)  # using std SysLogHandler
            elif self.transfer_method == 'slack':
                ret = self.slack(humepkt=humepkt, rowid=rowid)
            if ret is True:
                self.transfer_ok(rowid=rowid)
        return(True)

    def slack(self, humepkt=None, rowid=None):
        if humepkt is None or rowid is None:
            return(False)  # FIX: should not happen
        hume = humepkt['hume']['hume']
        ts = humepkt['ts']
        hume_hostname = hume['hostname']
        humed_hostname = self.humed_hostname
        if self.debug:
            pprinterr(hume)
        level = hume['level']
        tags = hume['tags']
        task = hume['task']
        msg = hume['msg']
        if tags is None:
            tagstr = ""
        else:
            tagstr = ','.join(tags)
        # Make sure to read:
        # https://api.slack.com/reference/surfaces/formatting
        m = "{hh} [{ts}] - {level} {host}:{task}: '{msg}' {tagstr}"
        m = m.format(hh=humed_hostname,
                     ts=ts,
                     level=level,
                     task=task,
                     host=hume_hostname,
                     msg=msg,
                     tagstr=tagstr)
        # https://api.slack.com/reference/surfaces/formatting#escaping
        m = m.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        # Remember, text becomes a fallback if 'blocks' are in use:
        # https://api.slack.com/messaging/composing/layouts#adding-blocks
        try:
            basetpl = self.transfer_method_args['template_base']
        except KeyError:
            basetpl = 'default'
        slackmsg = self.renderer.render(base_template=basetpl,
                                        level=level,
                                        humed_hostname=self.humed_hostname, # TODO: fix hostname thingy
                                        humePkt={'hume': hume})
        if slackmsg is None:
            # Fallback to text, no template worked
            if self.debug:
                printerr('Humed: no templates were available, fallbacking...')
            slackmsg = {'text', m, }  # TODO: move construction of m here
            data = json.dumps(slackmsg)
        else:
            data = slackmsg
        # choose appropriate channel by config key or task mapping
        task_channels = self.transfer_method_args.get('task_channels', {})
        webhook = None
        if isinstance(task_channels, dict) and task in task_channels:
            webhook = task_channels[task]
            chan = f'task_channels[{task}]'
        else:
            if level in ['ok', 'info']:
                chan = 'webhook_default'
            elif level in ['warning', 'unknown']:
                chan = 'webhook_warning'
            else:
                chan = 'webhook_{}'.format(level)
            if chan not in self.transfer_method_args.keys():
                chan = 'webhook_default'
            webhook = self.transfer_method_args[chan]
        if self.debug:
            printerr('Using {}="{}" for level "{}"'.format(chan,
                                                           webhook,
                                                           level))
        ret = requests.post(webhook,
                            headers={'Content-Type': 'application/json'},
                            data=data)
        if self.debug:
            pprinterr(ret)
        if ret.status_code == 200:
            return(True)
        return(False)

    def logstash(self, item=None):
        if item is None:
            return(False)  # FIX: should not happen
        rowid = item[0]
        ts = item[1]
        try:
            humepkt = json.loads(item[3])
        except Exception as ex:
            return(False)  # FIX: malformed json at this stage? mmm
        hume = humepkt['hume']
        sender_host = humepkt['hume']['hostname']
        if 'process' in humepkt.keys():  # This data is optional in hume (-a)
            process = humepkt['process']
        else:
            process = None
        # Extract info from hume to prepare logstash call
        # TODO: implement configuration for "LOG FORMAT" to use when sending
        level = hume['level']
        msg = hume['msg']
        task = hume['task']
        tags = hume['tags']
        humecmd = hume['humecmd']
        timestamp = hume['timestamp']
        # hume hostname
        hostname = hume['hostname']
        # extra field for logstash message
        extra = {
            'humed_hostname': sender_host,
            'hume_hostname': hume['hostname'],
            'tags': tags,
            'task': task,
            'humelevel': level,
            'humecmd': humecmd,
            'timestamp': timestamp
        }
        if process is not None:
            extra['process'] = process
        # Hume level does not relate completely, because 'ok' is not
        # a syslog severity, closest is info but...  TODO: think about this
        # hume level -> syslog severity
        # ----------------------------
        # ok         -> info (or default)
        # info       -> info (or default)
        # unknown    -> warning
        # warning    -> warning
        # error      -> error
        # critical   -> critical
        # debug      -> debug
        try:
            if level == 'ok' or level == 'info':
                # https://python-logstash-async.readthedocs.io/en/stable/usage.html#
                self.logger.info('hume({}): {}'.format(hostname, msg),
                                 extra=extra)
            elif level == 'warning' or level == 'unknown':
                self.logger.warning('hume({}) {}'.format(hostname, msg),
                                    extra=extra)
            elif level == 'error':
                self.logger.error('hume({}): {}'.format(hostname, msg),
                                  extra=extra)
            elif level == 'critical':
                self.logger.critical('hume({}): {}'.format(hostname, msg),
                                     extra=extra)
            elif level == 'debug':
                self.logger.debug('hume({}): {}'.format(hostname, msg),
                                  extra=extra)
        except Exception:  # TODO: improve exception handling
            return(False)
        else:
            return(True)

    def syslog(self, item=None):
        # This function handles both local and remote syslog
        # according to logging.handlers.SysLogHandler()
        if item is None:
            return(False)  # FIX: should not happen

        # Required data:
        rowid = item[0]
        ts = item[1]
        try:
            humepkt = json.loads(item[3])
        except Exception as ex:
            return(False)  # FIX: malformed json at this stage? mmm
        hume = humepkt['hume']
        sender_host = humepkt['hume']['hostname']

        # Optional data
        if 'process' in humepkt.keys():  # This data is optional in hume (-a)
            process = humepkt['process']
        else:
            process = None

        # Extract info from hume to prepare syslog message
        # TODO: decide if we should split these in the parent caller
        #       pros: tidier
        #       cons: makes development of other transfer methods
        #       more cumbersome? although... PLUGINS!
        level = hume['level']
        msg = hume['msg']
        task = hume['task']
        tags = hume['tags']
        humecmd = hume['humecmd']
        timestamp = hume['timestamp']
        # hostname
        # FIX: add a hostname configuration keyword
        # FIX: redundant code. more reasons to PLUGINS asap
        hostname = socket.getfqdn()

        # We dont have the 'extra' field for syslog, in contrast to logstash
        msg = '{} {} {} [{}] {} | TAGS={}'.format(sender_host,
                                                  task,
                                                  humelevel,
                                                  msg,
                                                  tags)
        if process is not None:
            msg = '{} PROC={}'.format(msg,
                                      json.dumps(extra['process']))
        else:
            msg = '{} PROC=None'.format(msg)
        # Hume level does not relate completely, because 'ok' is not
        # a syslog severity, closest is info but...  TODO: think about this
        # hume level -> syslog severity
        # ----------------------------
        # ok         -> info
        # info       -> info
        # unknown    -> warning
        # warn       -> warning
        # error      -> error
        # critical   -> critical
        # debug      -> debug
        try:
            if level == 'ok' or level == 'info':
                # https://python-logstash-async.readthedocs.io/en/stable/usage.html#
                self.logger.info('hume({}): {}'.format(hostname, msg))
            elif level == 'warning' or level == 'unknown':
                self.logger.warning('hume({}) {}'.format(hostname, msg))
            elif level == 'error':
                self.logger.error('hume({}): {}'.format(hostname, msg))
            elif level == 'critical':
                self.logger.critical('hume({}): {}'.format(hostname, msg))
            elif level == 'debug':
                self.logger.debug('hume({}): {}'.format(hostname, msg))
        except Exception:  # TODO: improve exception handling
            return(False)
        else:
            return(True)

    def is_valid_hume(self, hume):
        """Validate incoming hume packet structure and contents."""
        if not isinstance(hume, dict):
            return False
        if 'hume' not in hume or not isinstance(hume['hume'], dict):
            return False
        pkt = hume['hume']

        # version must exist and be supported
        if 'version' not in pkt or not isinstance(pkt['version'], int):
            return False
        if pkt['version'] not in SUPPORTED_MSG_VERSIONS:
            return False

        # timestamp must exist
        if 'timestamp' not in pkt or not isinstance(pkt['timestamp'], str):
            return False

        # hostname must exist and be valid
        if 'hostname' not in pkt or not is_valid_hostname(str(pkt['hostname'])):
            return False

        # level must be valid
        if 'level' not in pkt or pkt['level'] not in Hume.LEVELS:
            return False

        # message field must exist and be string
        if 'msg' not in pkt or not isinstance(pkt['msg'], str):
            return False

        # optional fields
        if 'tags' in pkt and not isinstance(pkt['tags'], list):
            return False
        if 'task' in pkt and not isinstance(pkt['task'], str):
            return False
        if 'extra' in pkt and not isinstance(pkt['extra'], dict):
            return False

        return True

    def check_auth_token(self, msg):
        """Return True if message token matches configured auth token."""
        if not self.auth_token:
            return True
        return msg.get('token') == self.auth_token

    def update_status(self, hume):
        """Store last known status per host/task"""
        try:
            pkt = hume['hume']
            host = str(pkt.get('hostname', ''))
            task = str(pkt.get('task', ''))
            level = str(pkt.get('level', ''))
            ts = pkt.get('timestamp')
            try:
                ts = datetime.datetime.fromisoformat(ts).timestamp()
            except Exception:
                ts = time.time()
            self.status[(host, task)] = (level, ts)
        except Exception:
            pass

    def render_metrics(self):
        lines = ['# TYPE hume_task_last_ts_seconds gauge']
        for (host, task), (level, ts) in self.status.items():
            h = host.replace('"', '\\"')
            t = task.replace('"', '\\"')
            l = level.replace('"', '\\"')
            lines.append(
                f'hume_task_last_ts_seconds{{hostname="{h}",task="{t}",level="{l}"}} {int(ts)}'
            )
        return '\n'.join(lines) + '\n'

    class _MetricsHandler(BaseHTTPRequestHandler):
        def __init__(self, humed, *args, **kwargs):
            self.humed = humed
            super().__init__(*args, **kwargs)

        def do_GET(self):
            if self.path == '/metrics':
                if self.humed.metrics_token:
                    auth = self.headers.get('Authorization', '')
                    if auth != f'Bearer {self.humed.metrics_token}':
                        self.send_response(403)
                        self.end_headers()
                        return
                data = self.humed.render_metrics().encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; version=0.0.4')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a, **kw):
            pass

    def start_metrics_server(self):
        if self.metrics_port is None:
            return
        handler = partial(self._MetricsHandler, self)
        self.metrics_server = HTTPServer(('0.0.0.0', self.metrics_port), handler)
        t = Thread(target=self.metrics_server.serve_forever)
        t.daemon = True
        t.start()

    def stop_metrics_server(self):
        if self.metrics_server:
            self.metrics_server.shutdown()
            self.metrics_server.server_close()

    def run(self):
        # Humed main loop
        sock = zmq.Context().socket(zmq.REP)
        sock.setsockopt(zmq.LINGER, 0)
        sock.bind(self.endpoint)
        # Check for pending transfers first
        self.queue.put(('work'))
        # Send 'ready' hume on debug channel
        msg = {'level': 'debug',
               'msg': 'Humed is ready to serve',
               'task': 'HUMED_STARTUP'}
        Hume(msg).send()
        # Await hume message over zmp and dispatch job thru queue
        while True:
            hume = {}
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            if poller.poll(1000):
                msg = sock.recv()
            else:
                continue
            try:
                hume = json.loads(msg)
            except Exception as ex:
                printerr(ex)
                printerr('Cannot json-loads the received message. notgood')
                sock.send_string('Invalid JSON message')
            except KeyboardInterrupt as kb:
                printerr('CTRL-C called, exiting now')
                sys.exit(255)
            else:
                if not self.check_auth_token(hume):
                    sock.send_string('AUTHFAIL')
                    continue
                # TODO: validate hume HERE and provide response accordingly
                # CLient MAY timeout before this happens so this SHOULD
                # NOT affect be a deal breaker
                sock.send_string('OK')
                hume.pop('token', None)
                if self.is_valid_hume(hume):
                    rowid = self.add_transfer(hume)  # TODO: verify ret
                    if self.debug:
                        printerr(rowid)
                    self.update_status(hume)
                    self.queue.put(('work'))
                else:
                    if self.debug:
                        printerr('Received hume is not valid:')
                        pprinterr(hume)


@pidfile()
def main():
    # First, parse configuration
    config = confuse.Configuration('humed')
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug',
                        default=False,
                        action='store_true',
                        help='Enables debug messages')
    parser.add_argument('--version',
                        action='version',
                        version='HumeDaemon v{} by Buanzo'.format(__version__))
    args = parser.parse_args()
    config.set_args(args)
    config.debug = args.debug
    try:
        valid = config.get(template=config_template)
    except confuse.NotFoundError as exc:
        printerr('Humed: Configuration validation results:')
        printerr('       {}'.format(exc))
        pass
    except Exception as ex:
        pprinterr(ex)
        printerr('Humed: Config file validation error: {}'.format(ex))
        sys.exit(2)
    if config.debug:
        printerr('-----[ CONFIG DUMP ]-----')
        printerr(config.dump())
        printerr('Available Transfer Methods: {}'.format(TRANSFER_METHODS))
        printerr('---[ CONFIG DUMP END ]---')

    # Initialize Stuff - configuration will be tested in Humed __init__
    humed = Humed(config=config)

    if config.debug:
        printerr('Ready. serving...')
    humed.run()


if __name__ == '__main__':
    # TODO: Add argparse and have master and slave modes
    main()

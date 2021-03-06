# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:

# Copyright (c) 2017, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in
#    the documentation and/or other materials provided with the
#    distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation
# are those of the authors and should not be interpreted as representing
# official policies, either expressed or implied, of the FreeBSD
# Project.
#
# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization that
# has cooperated in the development of these materials, makes any
# warranty, express or implied, or assumes any legal liability or
# responsibility for the accuracy, completeness, or usefulness or any
# information, apparatus, product, software, or process disclosed, or
# represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does not
# necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830
#}}}

from __future__ import absolute_import


import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from gevent import socket
from volttron.platform.agent import utils
from volttron.platform.vip.agent import Core, RPC
from energyplus.hacked_pubsub import SynchronizingPubSubAgent


utils.setup_logging()
_log = logging.getLogger(__name__)
SUCCESS = 'SUCCESS'
FAILURE = 'FAILURE'


class SocketServer():
    def __init__(self, **kwargs):
        self.sock = None
        self.size = 4096
        self.client = None
        self.sent = None
        self.rcvd = None
        self.host = None
        self.port = None

    def on_recv(self, msg):
        _log.debug('Received %s' % msg)

    def run(self):
        self.listen()

    def connect(self):
        if self.host is None:
            self.host = socket.gethostname()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.port is None:
            self.sock.bind((self.host, 0))
            self.port = self.sock.getsockname()[1]
        else:
            self.sock.bind((self.host, self.port))
        _log.debug('Bound to %r on %r' % (self.port, self.host))

    def send(self, msg):
        self.sent = msg
        if self.client is not None and self.sock is not None:
            try:
                self.client.send(self.sent)
            except Exception:
                _log.error('We got an error trying to send a message.')

    def recv(self):
        if self.client is not None and self.sock is not None:
            try:
                msg = self.client.recv(self.size)
            except Exception:
                _log.error('We got an error trying to read a message')
            return msg

    def start(self):
        _log.debug('Starting socket server')
        self.run()

    def stop(self):
        if self.sock != None:
            self.sock.close()

    def listen(self):
        self.sock.listen(10)
        _log.debug('server now listening')
        self.client, addr = self.sock.accept()
        _log.debug('Connected with ' + addr[0] + ':' + str(addr[1]))
        while True:
            msg = self.recv()
            if msg:
                self.rcvd = msg
                self.on_recv(msg)

    
class EnergyPlusAgent(SynchronizingPubSubAgent):
    def __init__(self, config_path, **kwargs):
        super(EnergyPlusAgent, self).__init__(config_path, **kwargs)
        self.version = 8.4
        self.bcvtb_home = '.'
        self.model = None
        self.weather = None
        self.socketFile = None
        self.variableFile = None
        self.self_advance_interval = None
        self.previous_timestamp = None
        self.first_publish = False
        self.advance_greenlet = None
        self.time = 0
        self.vers = 2
        self.flag = 0
        self.sent = None
        self.rcvd = None
        self.socket_server = None
        self.simulation = None
        self.step = None
        self.eplus_inputs = 0
        self.eplus_outputs = 0
        if not self.config:
            self.exit('No configuration found.')
        self.cwd = os.getcwd()


    @Core.receiver('onsetup')
    def setup(self, sender, **kwargs):
        super(EnergyPlusAgent, self).setup(sender, **kwargs)  
        
    @Core.receiver('onstart')
    def start(self, sender, **kwargs):
        self.subscribe()
        self.clear_last_update()
        self.start_socket_server()
        self.start_simulation()

    def start_socket_server(self):
        self.socket_server = SocketServer()
        self.socket_server.on_recv = self.recv_eplus_msg
        self.socket_server.connect()
        self.core.spawn(self.socket_server.start)    

    def start_simulation(self):
        if not self.model:
            self.exit('No model specified.')
        if not self.weather:
            self.exit('No weather specified.')
        model_path = self.model
        if model_path[0] == '~':
            model_path = os.path.expanduser(model_path)
        if model_path[0] != '/':
            model_path = os.path.join(self.cwd,model_path)
        weather_path = self.weather
        if weather_path[0] == '~':
            weather_path = os.path.expanduser(weather_path)
        if weather_path[0] != '/':
            weather_path = os.path.join(self.cwd,weather_path)
        model_dir = os.path.dirname(model_path)
        bcvtb_dir = self.bcvtb_home
        if bcvtb_dir[0] == '~':
            bcvtb_dir = os.path.expanduser(bcvtb_dir)
        if bcvtb_dir[0] != '/':
            bcvtb_dir = os.path.join(self.cwd,bcvtb_dir)
        _log.debug('Working in %r', model_dir)
        self.write_port_file(os.path.join(model_dir,'socket.cfg'))
        self.write_variable_file(os.path.join(model_dir,'variables.cfg'))
        if self.version >= 8.4:
            cmd_str = "cd %s; export BCVTB_HOME=%s; energyplus -w %s -r %s" % (model_dir, bcvtb_dir, weather_path, model_path)
        else:
            cmd_str = "export BCVTB_HOME=%s; runenergyplus %s %s" % (bcvtb_dir, model_path, weather_path)
        _log.debug('Running: %s', cmd_str)
        self.simulation = subprocess.Popen(cmd_str, shell=True)

    def send_eplus_msg(self):
        if self.socket_server:
            args = self.input()
            msg = '%r %r %r 0 0 %r' % (self.vers, self.flag, self.eplus_inputs, self.time)
            for obj in args.itervalues():
                if obj.get('name', None) and obj.get('type', None):
                    value = obj.get('value')
                    if isinstance(value, bool):
                        value = int(value)
                    msg = msg + ' ' + str(value)
                    _log.debug("Sending value {} ({}/{}): {}".format(obj.get("name"),
                                                                     obj.get("topic"),
                                                                     obj.get("field"), value))
            self.sent = msg + '\n'
            _log.info('Sending message to EnergyPlus: ' + msg)
            self.socket_server.send(self.sent)

    def recv_eplus_msg(self, msg):
        self.rcvd = msg
        self.parse_eplus_msg(msg)

        time_stamp_object_names = ("month", "day", "hour", "minute")

        time_stamp_objects = [self.output(name) for name in time_stamp_object_names]

        _now = None

        if all(time_stamp_objects):
            time_stamp_values = [obj.get("value") for obj in time_stamp_objects]
            if None not in time_stamp_values:
                # We handle minutes separately for eplus interface
                minutes = time_stamp_values[-1]
                _now = datetime(2017, *(int(round(x)) for x in time_stamp_values[:-1]))
                _now += timedelta(minutes=minutes)

        _log.info("Reported simulation timestamp: {}, previous: {}".format(_now, self.previous_timestamp))

        next_wait = 0.0
        if _now is not None and self.previous_timestamp is not None and _now > self.previous_timestamp:
            self.publish_all_outputs()
            long_wait = False

            # Always wait for commands after our first publish.
            if not self.first_publish:
                self.first_publish = True
                long_wait = True

            # Crossed the hour boundry.
            if _now.hour != self.previous_timestamp.hour:
                long_wait = True

            if long_wait or not self.simulation_pause:
                next_wait = self.self_advance_interval
        else:
            _log.debug("Skipping publish, model still warming up.")

        self.previous_timestamp = _now

        self.advance_greenlet = self.core.spawn_later(next_wait, self.advance_simulation)

    def parse_eplus_msg(self, msg):
        msg = msg.rstrip()
        _log.info('Received message from EnergyPlus: ' + msg)
        arry = msg.split()
        slot = 6
        flag = arry[1]
        output = self.output()
        if flag != '0':
            if flag == '1':
                self.exit('Simulation reached end: ' + flag)
            elif flag == '-1':
                self.exit('Simulation stopped with unspecified error: ' + flag)
            elif flag == '-10':
                self.exit('Simulation stopped with error during initialization: ' + flag)
            elif flag == '-20':
                self.exit('Simulation stopped with error during time integration: ' + flag)
#            else:
#                self.exit('Simulation stopped with error code ' + flag)
        elif ((arry[2] < self.eplus_outputs) and (len(arry) < self.eplus_outputs+6)):
            self.exit('Got message with ' + arry[2] + ' inputs. Expecting ' + str(self.eplus_outputs) + '.')
        else:
            if float(arry[5]): 
                self.time = float(arry[5])
            for key in output:
                if self.output(key, 'name') and self.output(key, 'type'):
                    try:
                        value = float(arry[slot])
                        _log.debug("{}: {}".format(key, value))
                        self.output(key, 'value', value)
                    except:
                        self.exit('Unable to convert received value to double.')
                    slot += 1

    def exit(self, msg):
        self.stop()
        _log.error(msg)
        sys.exit()

    def stop(self):
        if self.socket_server:
            self.socket_server.stop()
            self.socket_server = None

    def write_port_file(self, path):
        with open(path, "w+") as fh:
            fh.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n')
            fh.write('<BCVTB-client>\n')
            fh.write('  <ipc>\n')
            fh.write('    <socket port="%r" hostname="%s"/>\n' % (self.socket_server.port, self.socket_server.host))
            fh.write('  </ipc>\n')
            fh.write('</BCVTB-client>')

    def write_variable_file(self, path):
        with open(path, "w+") as fh:
            fh.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n')
            fh.write('<!DOCTYPE BCVTB-variables SYSTEM "variables.dtd">\n')
            fh.write('<BCVTB-variables>\n')
            for obj in self.output().itervalues():
                if obj.has_key('name') and obj.has_key('type'):
                    self.eplus_outputs = self.eplus_outputs + 1
                    fh.write('  <variable source="EnergyPlus">\n')
                    fh.write('    <EnergyPlus name="%s" type="%s"/>\n' % (obj.get('name'), obj.get('type')))
                    fh.write('  </variable>\n')
            for obj in self.input().itervalues():
                if obj.has_key('name') and obj.has_key('type'):
                    self.eplus_inputs = self.eplus_inputs + 1
                    fh.write('  <variable source="Ptolemy">\n')
                    fh.write('    <EnergyPlus %s="%s"/>\n' % (obj.get('type'), obj.get('name')))
                    fh.write('  </variable>\n')
            fh.write('</BCVTB-variables>\n')

    @RPC.export    
    def request_new_schedule(self, requester_id, task_id, priority, requests):
        """RPC method
        
        Requests one or more blocks on time on one or more device.
        In this agent, this does nothing!
        
        :param requester_id: Requester name. 
        :param task_id: Task name.
        :param priority: Priority of the task. Must be either HIGH, LOW, or LOW_PREEMPT
        :param requests: A list of time slot requests
        
        :type requester_id: str
        :type task_id: str
        :type priority: str
        :type request: list
        :returns: Request result
        :rtype: dict
        
        """
        _log.debug(requester_id + " requests new schedule " + task_id + " " + str(requests))
        result = {'result':SUCCESS, 
                  'data': {}, 
                  'info':''}
        return result

    @RPC.export 
    def request_cancel_schedule(self, requester_id, task_id):
        """RPC method
        
        Requests the cancelation of the specified task id.
        In this agent, this does nothing!
        
        :param requester_id: Requester name. 
        :param task_id: Task name.
        
        :type requester_id: str
        :type task_id: str
        :returns: Request result
        :rtype: dict
        
        """
        _log.debug(requester_id + " canceled " + task_id)
        result = {'result':SUCCESS,
                  'data': {},
                  'info': ''}
        return result   

    @RPC.export
    def get_point(self, topic, **kwargs):
        """RPC method
         
        Gets the value of a specific point on a device_name. 
        Does not require the device_name be scheduled. 
         
        :param topic: The topic of the point to grab in the 
                      format <device_name topic>/<point name>
        :param **kwargs: These get dropped on the floor
        :type topic: str
        :returns: point value
        :rtype: any base python type
         
        """
        obj = self.find_best_match(topic)
        if obj is not None: # we have an exact match to the  <device_name topic>/<point name>, so return the first value
            return obj.get('value', None)
        return None
            
    @RPC.export
    def set_point(self, requester_id, topic, value, **kwargs):
        """RPC method
        
        Sets the value of a specific point on a device. 
        Does not require the device be scheduled. 
        
        :param requester_id: Identifier given when requesting schedule. 
        :param topic: The topic of the point to set in the 
                      format <device topic>/<point name>
        :param value: Value to set point to.
        :param **kwargs: These get dropped on the floor
        :type topic: str
        :type requester_id: str
        :type value: any basic python type
        :returns: value point was actually set to.
        :rtype: any base python type
        
        """
        topic = topic.strip('/')
        result = self.update_topic_rpc(topic, value)
        _log.debug("Writing: {topic} : {value} {result}".format(topic=topic, value=value, result=result))
        if result==SUCCESS:
            return value
        else:
            raise RuntimeError("Failed to set value: " + result)

    @RPC.export
    def set_multiple_points(self, requester_id, topics_values, **kwargs):
        for topic, value in topics_values:
            topic = topic.strip('/')
            self.set_point(requester_id, topic, value)

        results = {}

        if self.simulation_pause:
            if self.advance_greenlet is not None:
                self.advance_greenlet.kill()
            self.advance_greenlet = self.core.spawn_later(0.0, self.advance_simulation)

        return results
     
    @RPC.export
    def revert_point(self, requester_id, topic, **kwargs):
        """RPC method
         
        Reverts the value of a specific point on a device to a default state. 
        Does not require the device be scheduled. 
         
        :param requester_id: Identifier given when requesting schedule. 
        :param topic: The topic of the point to revert in the 
                      format <device topic>/<point name>
        :param **kwargs: These get dropped on the floor
        :type topic: str
        :type requester_id: str
         
        """
        obj = self.find_best_match(topic)
        if obj and obj.has_key('default'):
            value = obj.get('default')
            _log.debug("Reverting topic "+topic+" to "+str(value))
            self.update_topic_rpc(topic, value)
        else:
            _log.warning("Unable to revert topic. No topic match or default defined!")

    @RPC.export
    def revert_device(self, requester_id, device_name, **kwargs): 
        """RPC method
         
        Reverts all points on a device to a default state. 
        Does not require the device be scheduled. 
         
        :param requester_id: Identifier given when requesting schedule. 
        :param topic: The topic of the device to revert (without a point!)
        :param **kwargs: These get dropped on the floor
        :type topic: str
        :type requester_id: str
         
        """
        device_name = device_name.strip('/')
        objs = self.get_inputs_from_topic(device_name) # we will assume that the topic is only the <device topic> and revert all matches at this level!
        if objs is not None:
            for obj in objs:
                point_name = obj.get('field', None)
                topic = device_name+"/" + point_name if point_name else device_name
                if obj.has_key('default'):
                    value = obj.get('default')
                    _log.debug("Reverting " + topic + " to " + str(value))
                    self.update_topic_rpc(topic, value)
                else:
                    _log.warning("Unable to revert " + topic + ". No default defined!")
    
    def update_topic_rpc(self, topic, value):
        obj = self.find_best_match(topic)
        if obj is not None:
            obj['value'] = value
            obj['last_update'] = datetime.utcnow().isoformat(' ') + 'Z'
            return SUCCESS
        return FAILURE
             
    def advance_simulation(self):
        _log.info('Advancing simulation.')
        for obj in self.input().itervalues():
            set_topic = obj['topic'] + '/' + obj['field']
            value = obj['value'] if obj.has_key('value') else obj['default']
            self.update_topic_rpc(set_topic, value)

        self.on_update_complete()
        return

    def on_update_complete(self):
        self.send_eplus_msg()



            

def main(argv=sys.argv):
    '''Main method called by the eggsecutable.'''
    try:
        utils.vip_main(EnergyPlusAgent)
    except Exception as e:
        _log.exception(e)


if __name__ == '__main__':
    # Entry point for script
    sys.exit(main())

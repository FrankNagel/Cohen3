#! /usr/bin/env python
#
# Licensed under the MIT license
# http://opensource.org/licenses/mit-license.php

# Copyright 2006, Frank Scholz <coherence@beebits.net>

import string
import socket
import os, sys

from zope.interface import implements, Interface
from twisted.python import log, filepath, util
from twisted.python.components import registerAdapter
from twisted.internet import task, address
from twisted.internet import reactor
from nevow import athena, inevow, loaders, tags, static
from twisted.web import resource

import louie

from coherence.upnp.core.ssdp import SSDPServer
from coherence.upnp.core.msearch import MSearch
from coherence.upnp.core.device import Device, RootDevice
from coherence.upnp.core.utils import parse_xml

from coherence.extern.logger import Logger
log = Logger('Coherence')

class IWeb(Interface):

    def goingLive(self):
        pass

class Web(object):

    def __init__(self, coherence):
        super(Web, self).__init__()
        self.coherence = coherence

class WebUI(athena.LivePage):
    """
    """

    addSlash = True
    docFactory = loaders.xmlstr("""\
<html xmlns:nevow="http://nevow.com/ns/nevow/0.1">
<head>
<nevow:invisible nevow:render="liveglue" />
<link rel="stylesheet" type="text/css" href="main.css" />
</head>
<body>
Coherence - a Python UPnP A/V framework
<p>
<div nevow:render="listchilds"></div>
</p>
</body>
</html>
""")

    def __init__(self, *a, **kw):
        super(WebUI, self).__init__( *a, **kw)
        self.coherence = self.rootObject.coherence

    def childFactory(self, ctx, name):
        log.info('WebUI childFactory: %s' % name)
        try:
            return self.rootObject.coherence.children[name]
        except:
            ch = super(WebUI, self).childFactory(ctx, name)
            if ch is None:
                p = util.sibpath(__file__, name)
                if os.path.exists(p):
                    ch = static.File(p)
            return ch
        
    def render_listchilds(self, ctx, data):
        cl = []
        log.info('children: %s' % self.coherence.children)
        for c in self.coherence.children:
            device = self.coherence.get_device_with_id(c)
            if device != None:
                _,_,_,device_type,version = device.get_device_type().split(':')
                cl.append( tags.li[tags.a(href='/'+c)[device_type,
                                                      ':',
                                                      version,
                                                      ' ',
                                                      device.get_friendly_name()]])
            else:
                cl.append( tags.li[c])
        return ctx.tag[tags.ul[cl]]
        
class WebServer:

    def __init__(self, port, coherence):
        from nevow import appserver

        def ResourceFactory( original):
            return WebUI( IWeb, original)

        registerAdapter(ResourceFactory, Web, inevow.IResource)

        self.web_root_resource = Web(coherence)
        #self.web_root_resource = inevow.IResource( web)
        #print self.web_root_resource
        self.site = appserver.NevowSite( self.web_root_resource)
        reactor.listenTCP( port, self.site)
        
        log.msg( "WebServer on port %d ready" % port)


class Coherence:

    def __init__(self):
        self.enable_log = False
        self.devices = []
        
        self.children = {}
        self._callbacks = {}
        
        logmode = 'info'        # FIXME: get this from the config file
                                #               and have one for every module
        self.enable_log = True  # FIXME: set this to True if a logmode != 'none'
        
        log.disable(name='Coherence')
        log.disable(name='SSDP')
        log.disable(name='MSEARCH')
        log.disable(name='Service')
        log.disable(name='MediaServer')
        log.disable(name='MediaRenderer')
        
        log.enable(name='Coherence')
        log.enable(name='SSDP')
        
        plugin = louie.TwistedDispatchPlugin()
        louie.install_plugin(plugin)

        log.msg("Coherence UPnP framework starting...")
        self.ssdp_server = SSDPServer()
        louie.connect( self.add_device, 'Coherence.UPnP.SSDP.new_device', louie.Any)
        louie.connect( self.remove_device, 'Coherence.UPnP.SSDP.remove_device', louie.Any)
        louie.connect( self.receiver, 'Coherence.UPnP.Device.detection_completed', louie.Any)
        #louie.connect( self.receiver, 'Coherence.UPnP.Service.detection_completed', louie.Any)

        self.ssdp_server.subscribe("new_device", self.add_device)
        self.ssdp_server.subscribe("removed_device", self.remove_device)

        self.msearch = MSearch(self.ssdp_server)

        reactor.addSystemEventTrigger( 'before', 'shutdown', self.shutdown)

        self.web_server_port = 30020
        self.hostname = socket.gethostbyname(socket.gethostname())
        #FIXME this doesn't work on systems with more than one network interface
        log.msg('running on host: %s' % self.hostname)
        self.urlbase = 'http://%s:%d/' % (self.hostname, self.web_server_port)

        self.web_server = WebServer( self.web_server_port, self)
                                
        self.renew_service_subscription_loop = task.LoopingCall(self.check_devices)
        self.renew_service_subscription_loop.start(20.0, now=False)


    
        # are we supposed to start a ControlPoint?
        try:
            from coherence.upnp.devices.control_point import ControlPoint
            #ControlPoint( self)
        except ImportError:
            log.msg("Can't enable ControlPoint functions, sub-system not available.")

        
        # are we supposed to start a MediaServer?
        try:
            from coherence.upnp.devices.media_server import MediaServer
            MediaServer( self,version=2)
        except ImportError:
            log.msg("Can't enable MediaServer functions, sub-system not available.")


        # are we supposed to start a MediaRenderer?
        try:
            from coherence.upnp.devices.media_renderer import MediaRenderer
            MediaRenderer( self)
        except ImportError:
            log.msg("Can't enable MediaRenderer functions, sub-system not available.")
        
    def receiver( self, signal, *args, **kwargs):
        #print "Coherence receiver called with", signal
        #print kwargs
        pass

    def shutdown( self):
        """ send service unsubscribe messages """
        try:
            self.renew_service_subscription_loop.stop()
        except:
            pass
        for root_device in self.get_devices():
            root_device.unsubscribe_service_subscriptions()
            for device in root_device.get_devices():
                device.unsubscribe_service_subscriptions()
        self.ssdp_server.shutdown()
        log.msg('Coherence UPnP framework shutdown')
        
    def check_devices(self):
        """ iterate over devices and their embedded ones and renew subscriptions """
        for root_device in self.get_devices():
            root_device.renew_service_subscriptions()
            for device in root_device.get_devices():
                device.renew_service_subscriptions()

    def subscribe(self, name, callback):
        self._callbacks.setdefault(name,[]).append(callback)

    def unsubscribe(self, name, callback):
        callbacks = self._callbacks.get(name,[])
        if callback in callbacks:
            callbacks.remove(callback)
        self._callbacks[name] = callbacks

    def callback(self, name, *args):
        for callback in self._callbacks.get(name,[]):
            callback(*args)


    def get_device_with_usn(self, usn):
        found = None
        for device in self.devices:
            if device.get_usn() == usn:
                found = device
                break
        return found

    def get_device_with_id(self, device_id):
        found = None
        for device in self.devices:
            id = device.get_id()
            if device_id[:5] != 'uuid:':
                id = id[5:]
            if id == device_id:
                found = device
                break
        return found

    def get_devices(self):
        return self.devices

    def add_device(self, device_type, infos):
        if infos['ST'] == 'upnp:rootdevice':
            root = RootDevice(infos)
            self.devices.append(root)
        else:
            root_id = infos['USN'][:-len(infos['ST'])-2]
            root = self.get_device_with_id(root_id)
            device = Device(infos, root)
        # fire this only after the device detection is fully completed
        # and we are on the device level already, so we can work with them instead with the SSDP announce
        #if infos['ST'] == 'upnp:rootdevice':
        #    self.callback("new_device", infos['ST'], infos)


    def remove_device(self, device_type, infos):
        device = self.get_device_with_usn(infos['USN'])
        if device:
            self.devices.remove(device)
            if infos['ST'] == 'upnp:rootdevice':
                self.callback("removed_device", infos['ST'], infos['USN'])

            
    def add_web_resource(self, name, sub):
        #self.web_server.web_root_resource.putChild(name, sub)
        self.children[name] = sub
        #print self.web_server.web_root_resource.children

    def remove_web_resource(self, name):
        # XXX implement me
        pass
        
def main():

    # get settings or options
    Coherence()
    reactor.run()
    
if __name__ == '__main__':
    main()

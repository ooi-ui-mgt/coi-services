#!/usr/bin/env python

"""
@package ion.agents.platform.oms.oms_platform_driver
@file    ion/agents/platform/oms/oms_platform_driver.py
@author  Carlos Rueda
@brief   Base class for OMS platform drivers.
"""

__author__ = 'Carlos Rueda'
__license__ = 'Apache 2.0'


from pyon.public import log

from ion.agents.platform.platform_driver import PlatformDriver
from ion.agents.platform.exceptions import PlatformException
from ion.agents.platform.exceptions import PlatformDriverException
from ion.agents.platform.exceptions import PlatformConnectionException
from ion.agents.platform.oms.oms_resource_monitor import OmsResourceMonitor
from ion.agents.platform.oms.oms_client_factory import OmsClientFactory
from ion.agents.platform.oms.oms_client import InvalidResponse
from ion.agents.platform.util.network import NNode
from ion.agents.platform.util.network import Attr
from ion.agents.platform.util.network import Port


class OmsPlatformDriver(PlatformDriver):
    """
    Base class for OMS platform drivers.
    """

    def __init__(self, platform_id, driver_config, parent_platform_id=None):
        """
        Creates an OmsPlatformDriver instance.

        @param platform_id Corresponding platform ID
        @param driver_config with required 'oms_uri' entry.
        @param parent_platform_id Platform ID of my parent, if any.
                    This is mainly used for diagnostic purposes
        """
        PlatformDriver.__init__(self, platform_id, driver_config, parent_platform_id)

        if not 'oms_uri' in driver_config:
            raise PlatformDriverException(msg="driver_config does not indicate 'oms_uri'")

        oms_uri = driver_config['oms_uri']
        log.debug("%r: creating OmsClient instance with oms_uri=%r",
            self._platform_id, oms_uri)
        self._oms = OmsClientFactory.create_instance(oms_uri)
        log.debug("%r: OmsClient instance created: %s",
            self._platform_id, self._oms)

        # TODO set-up configuration for notification of events associated
        # with values retrieved during platform resource monitoring

        # _monitors: dict { attr_id: OmsResourceMonitor }
        self._monitors = {}

    def ping(self):
        """
        Verifies communication with external platform returning "PONG" if
        this verification completes OK.

        @retval "PONG" iff all OK.
        @raise PlatformConnectionException Cannot ping external platform or
               got unexpected response.
        """
        log.debug("%r: pinging OMS...", self._platform_id)
        try:
            retval = self._oms.hello.ping()
        except Exception, e:
            raise PlatformConnectionException(msg="Cannot ping %s" % str(e))

        if retval is None or retval.upper() != "PONG":
            raise PlatformConnectionException(msg="Unexpected ping response: %r" % retval)

        return "PONG"

    def go_active(self):
        """
        Main task here is to determine the topology of platforms
        rooted here then assigning the corresponding definition to self._nnode.

        @raise PlatformConnectionException
        """

        # NOTE: The following log.debug DOES NOT show up when running a test
        # with the pycc plugin (--with-pycc)!  (noticed with test_oms_launch).
        log.debug("%r: going active..", self._platform_id)

        # note, we ping the OMS here regardless of the source for the network
        # definition:
        self.ping()

        if self._topology:
            self._nnode = self._build_network_definition_using_topology()
        else:
            self._nnode = self._build_network_definition_using_oms()

        log.debug("%r: go_active completed ok. _nnode:\n%s",
                 self._platform_id, self._nnode.dump())

        self.__gen_diagram()

    def __gen_diagram(self):
        """
        Convenience method for testing/debugging.
        Generates a dot diagram iff the environment variable GEN_DIAG is
        defined and this driver corresponds to the root of the network
        (determined by not having a parent platform ID).
        """
        try:
            import os, tempfile, subprocess
            if os.getenv("GEN_DIAG", None) is None:
                return
            if self._parent_platform_id:
                # I'm not the root of the network
                return

            # I'm the root of the network
            name = self._platform_id
            base_name = '%s/%s' % (tempfile.gettempdir(), name)
            dot_name = '%s.dot' % base_name
            png_name = '%s.png' % base_name
            print 'generating diagram %r' % dot_name
            file(dot_name, 'w').write(self._nnode.diagram(style="dot"))
            print 'generating png %r' % png_name
            dot_cmd = 'dot -Tpng %s -o %s' % (dot_name, png_name)
            subprocess.call(dot_cmd.split())
            print 'opening %r' % png_name
            open_cmd = 'open %s' % png_name
            subprocess.call(open_cmd.split())
        except Exception, e:
            print "error generating or opening diagram: %s" % str(e)

    def _build_network_definition_using_topology(self):
        """
        Uses self._topology to build the network definition.
        """
        log.debug("%r: _build_network_definition_using_topology: %s",
            self._platform_id, self._topology)

        def build(platform_id, children):
            """
            Returns the root NNode for the given platform_id with its
            children according to the given list.
            """
            nnode = NNode(platform_id)
            self._set_attributes_and_ports_from_agent_device_map(nnode)

            log.debug('Created NNode for %r', platform_id)

            for subplatform_id in children:
                subplatform_children = self._topology.get(subplatform_id, [])
                sub_nnode = build(subplatform_id, subplatform_children)
                nnode.add_subplatform(sub_nnode)

            return nnode

        children = self._topology.get(self._platform_id, [])
        return build(self._platform_id, children)

    def _set_attributes_and_ports_from_agent_device_map(self, nnode):
        """
        Sets the attributes and ports for the given NNode from
        self._agent_device_map if not None.
        """
        if self._agent_device_map is None:
            return

        platform_id = nnode.platform_id
        if platform_id not in self._agent_device_map:
            log.warn("%r: no entry in agent_device_map for platform_id",
                     self._platform_id, platform_id)
            return

        device_obj = self._agent_device_map[platform_id]
        log.info("%r: for platform_id=%r device_obj=%s",
                    self._platform_id, platform_id, device_obj)

        attrs = device_obj.platform_monitor_attributes
        ports = device_obj.ports

        for attr_obj in attrs:
            attr = Attr(attr_obj.id, {
                'name': attr_obj.name,
                'monitorCycleSeconds': attr_obj.monitor_rate,
                'units': attr_obj.units,
                })
            nnode.add_attribute(attr)

        for port_obj in ports:
            port = Port(port_obj.port_id, port_obj.ip_address)
            nnode.add_port(port)

    def _build_network_definition_using_oms(self):
        """
        Uses OMS to build the network definition.
        """
        log.debug("%r: _build_network_definition_using_oms..", self._platform_id)
        try:
            map = self._oms.config.getPlatformMap()
        except Exception, e:
            log.debug("%r: error getting platform map %s", self._platform_id, str(e))
            raise PlatformConnectionException(msg="error getting platform map %s" % str(e))

        log.debug("%r: got platform map %s", self._platform_id, str(map))

        def build_network_definition(map):
            """
            Returns the root NNode according to self._platform_id and the OMS'
            getPlatformMap response.
            """
            nodes = NNode.create_network(map)
            if not self._platform_id in nodes:
                msg = "platform map does not contain entry for %r" % self._platform_id
                log.error(msg)
                raise PlatformException(msg=msg)

            return nodes[self._platform_id]

        return build_network_definition(map)

    def get_attribute_values(self, attr_names, from_time):
        """
        """
        retval = self._oms.getPlatformAttributeValues(self._platform_id, attr_names, from_time)
        log.debug("getPlatformAttributeValues = %s", retval)

        if not self._platform_id in retval:
            raise PlatformException("Unexpected: response does not include "
                                    "requested platform '%s'" % self._platform_id)

        attr_values = retval[self._platform_id]
        return attr_values

    def _verify_platform_id_in_response(self, response):
        """
        Verifies the presence of my platform_id in the response.

        @param response Dictionary returned by _oms

        @retval response[self._platform_id]
        """
        if not self._platform_id in response:
            msg = "unexpected: response does not contain entry for %r" % self._platform_id
            log.error(msg)
            raise PlatformException(msg=msg)

        if response[self._platform_id] == InvalidResponse.PLATFORM_ID:
            #
            # TODO Note, this should normally be an error; but I'm just
            # logging a warning because at this moment there's a mix of
            # information sources: topology from a dictionary but some other
            # pieces from OMS, like platform attributes.
            #
            log.warn("response reports invalid platform_id for %r", self._platform_id)
            return None
        else:
            return response[self._platform_id]

    def start_resource_monitoring(self):
        """
        Starts greenlets to periodically retrieve values of the attributes
        associated with my platform, and do corresponding event notifications.
        """
        log.debug("%r: getting platform attributes", self._platform_id)

        attrs = self._oms.getPlatformAttributes(self._platform_id)
        log.debug("%r: getPlatformAttributes=%s",
            self._platform_id, attrs)

        attr_info = self._verify_platform_id_in_response(attrs)

        if not attr_info:
            # no attributes to monitor.
            log.debug("%r: NOT starting resource monitoring", self._platform_id)
            return

        log.debug("%r: starting resource monitoring", self._platform_id)

        #
        # TODO attribute grouping so one single greenlet is launched for a
        # group of attributes having same or similar monitoring rate. For
        # simplicity at the moment, start a greenlet per attribute.
        #

        for attr_name, attr_defn in attr_info.iteritems():
            if 'monitorCycleSeconds' in attr_defn:
                self._start_monitor_greenlet(attr_defn)
            else:
                log.warn(
                    "%r: unexpected: attribute info does not contain %r "
                    "for attribute %r. attr_defn = %s",
                        self._platform_id,
                        'monitorCycleSeconds', attr_name, str(attr_defn))

    def _start_monitor_greenlet(self, attr_defn):
        assert 'attr_id' in attr_defn
        assert 'monitorCycleSeconds' in attr_defn
        resmon = OmsResourceMonitor(self._oms, self._platform_id, attr_defn,
                                    self._notify_driver_event)
        self._monitors[attr_defn['attr_id']] = resmon
        resmon.start()

    def stop_resource_monitoring(self):
        """
        Stops all the monitoring greenlets.
        """
#        assert self._oms is not None, "set_oms_client must have been called"

        log.debug("%r: stopping resource monitoring", self._platform_id)
        for resmon in self._monitors.itervalues():
            resmon.stop()
        self._monitors.clear()

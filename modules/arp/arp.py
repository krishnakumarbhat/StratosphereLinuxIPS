# Must imports
from slips_files.common.abstracts import Module
import multiprocessing
from slips_files.core.database import __database__
from slips_files.common.slips_utils import utils
import configparser
import signal, os

# Your imports
import json
import sys
import datetime
import ipaddress
import time


class Module(Module, multiprocessing.Process):
    # Name: short name of the module. Do not use spaces
    name = 'arp'
    description = 'Detect arp attacks'
    authors = ['Alya Gomaa']

    def __init__(self, outputqueue, config, redis_port):
        multiprocessing.Process.__init__(self)
        # All the printing output should be sent to the outputqueue.
        # The outputqueue is connected to another process called OutputProcess
        self.outputqueue = outputqueue
        # In case you need to read the slips.conf configuration file for
        # your own configurations
        self.config = config
        # Start the DB
        __database__.start(self.config, redis_port)
        self.c1 = __database__.subscribe('new_arp')
        self.c2 = __database__.subscribe('tw_closed')
        self.timeout = 0.0000001
        self.read_configuration()
        # this dict will categorize arp requests by profileid_twid
        self.cache_arp_requests = {}
        # Threshold to use to detect a port scan. How many arp minimum are required?
        self.arp_scan_threshold = 5
        # get the default gateway
        self.gateway = __database__.get_gateway_ip()
        self.delete_arp_periodically = False
        self.arp_ts = 0
        self.period_before_deleting = 0
        if (
            'yes' in self.delete_zeek_files
            and 'no' in self.store_zeek_files_copy
        ):
            self.delete_arp_periodically = True
            # first time arp.log is created
            self.arp_ts = time.time()
            # in seconds
            self.period_before_deleting = 3600

    def print(self, text, verbose=1, debug=0):
        """
        Function to use to print text using the outputqueue of slips.
        Slips then decides how, when and where to print this text by taking all the processes into account
        :param verbose:
            0 - don't print
            1 - basic operation/proof of work
            2 - log I/O operations and filenames
            3 - log database/profile/timewindow changes
        :param debug:
            0 - don't print
            1 - print exceptions
            2 - unsupported and unhandled types (cases that may cause errors)
            3 - red warnings that needs examination - developer warnings
        :param text: text to print. Can include format like 'Test {}'.format('here')
        """

        levels = f'{verbose}{debug}'
        self.outputqueue.put(f'{levels}|{self.name}|{text}')

    def read_configuration(self):
        self.home_network = []
        try:
            self.home_network.append(
                self.config.get('parameters', 'home_network')
            )
        except (
            configparser.NoOptionError,
            configparser.NoSectionError,
            NameError,
        ):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.home_network = utils.home_network_ranges
        # convert the ranges into network obj
        self.home_network = list(map(ipaddress.ip_network, self.home_network))

        try:
            self.delete_zeek_files = self.config.get(
                'parameters', 'delete_zeek_files'
            )
        except (
            configparser.NoOptionError,
            configparser.NoSectionError,
            NameError,
        ):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.delete_zeek_files = 'no'

        try:
            self.store_zeek_files_copy = self.config.get(
                'parameters', 'store_a_copy_of_zeek_files'
            )
        except (
            configparser.NoOptionError,
            configparser.NoSectionError,
            NameError,
        ):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.store_zeek_files_copy = 'yes'

    def check_arp_scan(
        self, profileid, twid, daddr, uid, ts, dst_mac, src_mac
    ):
        """
        Check if the profile is doing an arp scan
        If IP X sends arp requests to 3 or more different IPs within 30 seconds, then this IP X is doing arp scan
        The key profileid_twid is used to group requests from the same saddr
        arp flows don't have uids, the uids received are randomly generated by slips
        """

        # The Gratuitous arp is sent as a broadcast, as a way for a node to announce or update its IP to MAC mapping to the entire network.
        # It shouldn't be marked as an arp scan
        saddr = profileid.split('_')[1]

        # Dont detect arp scan from the GW router
        # Don't use 'in' since 192.168.1.1 is in 192.168.1.117 and that is wrong
        if self.gateway == saddr:
            return False
        if saddr == '0.0.0.0':
            return False

        try:
            # Get together all the arp requests for each IP in this TW
            cached_requests = self.cache_arp_requests[f'{profileid}_{twid}']
            # Append the arp request, and when it happened
            cached_requests.update({daddr: {'uid': uid, 'ts': ts}})
            # Update the dict
            self.cache_arp_requests[f'{profileid}_{twid}'] = cached_requests
        except KeyError:
            # create the key for this profileid_twid if it doesn't exist
            self.cache_arp_requests[f'{profileid}_{twid}'] = {
                daddr: {'uid': uid, 'ts': ts}
            }
            return True

        profileids_twids = list(cached_requests.keys())
        # The minimum amount of arp packets to send to be considered as scan is 5
        if len(profileids_twids) >= self.arp_scan_threshold:
            # check if these requests happened within 30 secs
            # get the first and the last request of the 10
            first_daddr = profileids_twids[0]
            last_daddr = profileids_twids[-1]
            starttime = cached_requests[first_daddr]['ts']
            endtime = cached_requests[last_daddr]['ts']
            # get the time of each one in seconds
            # todo do we need mac addresses?
            starttime = datetime.datetime.fromtimestamp(starttime)
            endtime = datetime.datetime.fromtimestamp(endtime)
            # get the difference between them in seconds
            self.diff = float(str(endtime - starttime).split(':')[-1])
            if self.diff <= 30.00:
                # we are sure this is an arp scan
                confidence = 0.8
                threat_level = 'low'
                description = (
                    f'performing an arp scan. Confidence {confidence}.'
                )
                type_evidence = 'ARPScan'
                # category of this evidence according to idea categories
                category = 'Recon.Scanning'
                type_detection = 'srcip'
                source_target_tag = 'Recon'  # srcip description
                detection_info = profileid.split('_')[1]
                conn_count = len(profileids_twids)
                __database__.setEvidence(
                    type_evidence,
                    type_detection,
                    detection_info,
                    threat_level,
                    confidence,
                    description,
                    ts,
                    category,
                    source_target_tag=source_target_tag,
                    conn_count=conn_count,
                    profileid=profileid,
                    twid=twid,
                    uid=uid,
                )
                # after we set evidence, clear the dict so we can detect if it does another scan
                self.cache_arp_requests.pop(f'{profileid}_{twid}')
                return True
        return False

    def check_dstip_outside_localnet(
        self, profileid, twid, daddr, uid, saddr, ts
    ):
        """Function to setEvidence when daddr is outside the local network"""

        if '0.0.0.0' in saddr or '0.0.0.0' in daddr:
            # this is the case of arp probe, not an arp outside of local network, don't alert
            return False

        daddr_as_obj = ipaddress.IPv4Address(daddr)
        if daddr_as_obj.is_multicast or daddr_as_obj.is_link_local:
            # The arp to ‘outside’ the network should not detect multicast or link-local addresses.
            return False

        for network in self.home_network:
            if daddr_as_obj in network:
                # IP is in this local network, don't alert
                return False

        # to prevent arp alerts from one IP to itself
        local_net = saddr.split('.')[0]
        if not daddr.startswith(local_net):
            # comes here if the IP isn't in any of the local networks
            confidence = 0.6
            threat_level = 'low'
            ip_identification = __database__.getIPIdentification(daddr)
            description = f'{saddr} sending arp packet to a destination address outside of local network: {daddr}. {ip_identification}'
            type_evidence = 'arp-outside-localnet'
            category = 'Anomaly.Behaviour'
            type_detection = 'srcip'
            detection_info = profileid.split('_')[1]
            __database__.setEvidence(
                type_evidence,
                type_detection,
                detection_info,
                threat_level,
                confidence,
                description,
                ts,
                category,
                profileid=profileid,
                twid=twid,
                uid=uid,
            )
            return True

    def detect_unsolicited_arp(
        self, profileid, twid, uid, ts, dst_mac, src_mac, dst_hw, src_hw
    ):
        """Unsolicited arp is used to update the neighbours' arp caches but can also be used in arp spoofing"""
        if (
            dst_mac == 'ff:ff:ff:ff:ff:ff'
            and dst_hw == 'ff:ff:ff:ff:ff:ff'
            and src_mac != '00:00:00:00:00:00'
            and src_hw != '00:00:00:00:00:00'
        ):
            # We're sure this is unsolicited arp
            confidence = 0.8
            threat_level = 'info'
            description = 'detected sending unsolicited arp'
            type_evidence = 'UnsolicitedARP'
            # This may be arp spoofing
            category = 'Information'
            type_detection = 'srcip'
            source_target_tag = 'Recon'   # srcip description
            detection_info = profileid.split('_')[1]
            __database__.setEvidence(
                type_evidence,
                type_detection,
                detection_info,
                threat_level,
                confidence,
                description,
                ts,
                category,
                source_target_tag=source_target_tag,
                profileid=profileid,
                twid=twid,
                uid=uid,
            )
            return True

    def detect_MITM_ARP_attack(self, profileid, twid, uid, saddr, ts, src_mac):
        """Detects when a MAC with IP A, is trying to tell others that now that MAC is also for IP B (arp cache attack)"""

        # to test this add these 2 flows to arp.log
        # {"ts":1636305825.755132,"operation":"reply","src_mac":"2e:a4:18:f8:3d:02","dst_mac":"ff:ff:ff:ff:ff:ff","orig_h":"172.20.7.40","resp_h":"172.20.7.40","orig_hw":"2e:a4:18:f8:3d:02","resp_hw":"00:00:00:00:00:00"}
        # {"ts":1636305825.755132,"operation":"reply","src_mac":"2e:a4:18:f8:3d:02","dst_mac":"ff:ff:ff:ff:ff:ff","orig_h":"172.20.7.41","resp_h":"172.20.7.41","orig_hw":"2e:a4:18:f8:3d:02","resp_hw":"00:00:00:00:00:00"}

        # todo will we get FPs when an ip changes?
        # todo what if the ip of the attacker came to us first and we stored it in the db? the original IP of this src mac is now the IP of the attacker?

        # get the original IP of the src mac from the database
        original_IP = __database__.get_IP_of_MAC(src_mac)
        if original_IP == None:
            return

        # original_IP is a serialized list
        original_IP = json.loads(original_IP)[0]

        # is this saddr trying to tell everyone that this it owns this src_mac
        # even though we know this src_mac is associated with another IP (original_IP)?
        if saddr != original_IP:
            # From our db we know that:
            # original_IP has src_MAC
            # now saddr has src_MAC and saddr isn't the same as original_IP
            # so this is either a MITM arp attack or the IP address of this src_mac simply changed
            # todo how to find out which one is it??
            confidence = 0.2   # low confidence for now
            threat_level = 'ciritical'
            description = f'{saddr} performing a MITM arp attack. The MAC {src_mac}, now belonging to IP {saddr}, was seen before for IP {original_IP}.'
            # self.print(f'{saddr} is claiming to have {src_mac}')
            type_evidence = 'MITM-arp-attack'
            # This may be arp spoofing
            category = 'Recon'
            type_detection = 'srcip'
            source_target_tag = 'MITM'
            detection_info = profileid.split('_')[1]
            __database__.setEvidence(
                type_evidence,
                type_detection,
                detection_info,
                threat_level,
                confidence,
                description,
                ts,
                category,
                source_target_tag=source_target_tag,
                profileid=profileid,
                twid=twid,
                uid=uid,
            )
            return True

    def shutdown_gracefully(self):
        # Confirm that the module is done processing
        __database__.publish('finished_modules', self.name)

    def run(self):
        utils.drop_root_privs()
        # Main loop function
        while True:
            try:
                if (
                    self.delete_arp_periodically
                    and time.time()
                    >= self.arp_ts + self.period_before_deleting
                ):
                    # clear arp.log
                    open('zeek_files/arp.log', 'w').close()
                    # update ts of the new arp.log
                    self.arp_ts = time.time()

                message = self.c1.get_message(timeout=self.timeout)
                if message and message['data'] == 'stop_process':
                    self.shutdown_gracefully()
                    return True

                if utils.is_msg_intended_for(message, 'new_arp'):
                    flow = json.loads(message['data'])
                    ts = flow['ts']
                    profileid = flow['profileid']
                    twid = flow['twid']
                    daddr = flow['daddr']
                    saddr = flow['saddr']
                    dst_mac = flow['dst_mac']
                    src_mac = flow['src_mac']
                    dst_hw = flow['dst_hw']
                    src_hw = flow['src_hw']
                    operation = flow['operation']
                    # arp flows don't have uids, the uids received are randomly generated by slips
                    uid = flow['uid']

                    # The Gratuitous arp is sent as a broadcast, as a way for a node to announce or update
                    # its IP to MAC mapping to the entire network.
                    #  Gratuitous ARP shouldn't be marked as an arp scan
                    is_gratuitous = saddr == daddr and dst_mac in [
                        'ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00', src_mac]

                    if is_gratuitous:
                        # for MITM arp attack, the arp has to be gratuitous
                        # and it has to be a reply operation, not a request.
                        if 'reply' in operation:
                            self.detect_MITM_ARP_attack(
                                profileid, twid, uid, saddr, ts, src_mac
                            )
                    else:
                        # not gratuitous, may be an arp scan
                        self.check_arp_scan(
                            profileid, twid, daddr, uid, ts, dst_mac, src_mac
                        )

                    if 'request' in operation:
                        self.check_dstip_outside_localnet(
                            profileid, twid, daddr, uid, saddr, ts
                        )
                    elif 'reply' in operation:
                        # Unsolicited ARPs should be of type reply only, not request
                        self.detect_unsolicited_arp(
                            profileid,
                            twid,
                            uid,
                            ts,
                            dst_mac,
                            src_mac,
                            dst_hw,
                            src_hw,
                        )

                # if the tw is closed, remove all its entries from the cache dict
                message = self.c2.get_message(timeout=self.timeout)
                if message and message['data'] == 'stop_process':
                    self.shutdown_gracefully()
                    return True

                if utils.is_msg_intended_for(message, 'tw_closed'):
                    profileid_tw = message['data']
                    # when a tw is closed, this means that it's too old so we don't check for arp scan in this time range anymore
                    # this copy is made to avoid dictionary changed size during iteration err
                    cache_copy = self.cache_arp_requests.copy()
                    for key in cache_copy:
                        if profileid_tw in key:
                            self.cache_arp_requests.pop(key)
                            # don't break, keep looking for more keys that belong to the same tw

            except KeyboardInterrupt:
                self.shutdown_gracefully()
                return True
            except Exception as inst:
                exception_line = sys.exc_info()[2].tb_lineno
                self.print(f'Problem on the run() line {exception_line}', 0, 1)
                self.print(str(type(inst)), 0, 1)
                self.print(str(inst.args), 0, 1)
                self.print(str(inst), 0, 1)
                return True

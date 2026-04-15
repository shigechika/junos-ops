#!/usr/bin/env python3
# -*- mode: python; python-indent-offset: 4 -*-
#
#   Copyright ©︎2023 AIKAWA Shigechika
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

# Hi-Speeed Collection of JUNOS Request Support Information

from concurrent import futures
from distutils.version import LooseVersion
from jnpr.junos import Device
from jnpr.junos.exception import (
    ConnectAuthError,
    ConnectClosedError,
    ConnectError,
    ConnectRefusedError,
    ConnectTimeoutError,
    ConnectUnknownHostError,
)
from jnpr.junos.exception import RpcError, RpcTimeoutError
from jnpr.junos.utils.config import Config
from jnpr.junos.utils.fs import FS
from jnpr.junos.utils.sw import SW
from lxml import etree
from ncclient.operations.errors import TimeoutExpiredError
from pprint import pprint
import argparse
import configparser
import datetime
import os
import re
import sys


class rsi:
    version = "0.0"

    def __init__(self):
        self.parse_arg()
        self.parse_config()
        self.get_targets()

    def parse_arg(self):
        parser = argparse.ArgumentParser(
            description="Hi-Speeed Collection of JUNOS Configuration and Request Support Information",
            epilog="default action is show device facts",
        )
        parser.add_argument(
            "specialhosts",
            metavar="hostname",
            type=str,
            nargs="*",
            help="special hostname(s)",
        )
        parser.add_argument(
            "--recipe",
            default="junos.ini",
            type=str,
            help="junos recipe filename (default: %(default)s). PRIORITY 1st this option, 2nd $PWD/junos.ini, 3rd $HOME/.junos.ini",
        )
        parser.add_argument(
            "--list",
            "-ls",
            action="store_const",
            dest="list_format",
            const="short",
            help="short list remote path (like as ls)",
        )
        parser.add_argument(
            "--dryrun",
            action="store_true",
            help="test for --copy/--install/--update. connect and message output. No execute.",
        )
        parser.add_argument(
            "--showversion",
            "--version",
            action="store_true",
            help="show running/planning/pending version and reboot schedule",
        )
        parser.add_argument("-d", "--debug", action="store_true", help="for debug")
        parser.add_argument("-V", action="version", version="%(prog)s " + self.version)
        self.args = parser.parse_args()

    def parse_config(self):
        self.config = configparser.ConfigParser(allow_no_value=True)
        # config file priolity
        # 1st option, 2nd $CWD/junos.ini, 3rd $HOME/.junos.ini
        if os.path.isfile(self.args.recipe):
            self.config_ini = self.args.recipe
        elif os.path.isfile(f"{os.getcwd()}/junos.ini"):
            self.config_ini = f"{os.getcwd()}/junos.ini"
        else:
            self.config_ini = f"{os.environ['HOME']}/.junos.ini"
        self.config.read(self.config_ini)
        if self.args.debug:
            if len(self.config.sections()) == 0:
                raise ValueError(f"{self.args.recipe} is empty")
        for section in self.config.sections():
            if self.config.has_option(section, "host"):
                host = self.config.get(section, "host")
            else:
                host = None
            if host is None:
                # host is [section] name
                self.config.set(section, "host", section)
            # if self.args.debug:
            #    for key in self.config[section]:
            #        print(f"{section} > {key} : {self.config[section][key]}")

    def get_targets(self):
        self.targets = []
        if len(self.args.specialhosts) == 0:
            for i in self.config.sections():
                tmp = self.config.get(i, "host")
                # if self.args.debug:
                #    print(i, tmp)
                if tmp is not None:
                    self.targets.append(i)
                else:
                    print(i, "is not found in", self.args.recipe)
                    sys.exit(1)
        else:
            for i in self.args.specialhosts:
                if self.config.has_section(i):
                    tmp = self.config.get(i, "host")
                else:
                    print(i, "is not found in", self.args.recipe)
                    sys.exit(1)
                # if self.args.debug:
                #    print(i, tmp)
                self.targets.append(i)

    def connect(self, hostname):
        if self.args.debug:
            print("connect: start", flush=True)
        dev = Device(
            host=self.config.get(hostname, "host"),
            port=self.config.get(hostname, "port", fallback=830),
            user=self.config.get(hostname, "id"),
            passwd=self.config.get(hostname, "pw"),
            ssh_private_key_file=self.config.get(hostname, "sshkey"),
            huge_tree=self.config.get(hostname, "huge_tree", fallback=False),
        )
        try:
            dev.open()
        except Exception as e:
            print(e)
            dev = None
        if self.args.debug:
            print(f"dev = {dev}")
        if self.args.debug:
            print("connect: end", flush=True)
        return dev

    def get_support_information(self, dev):
        try:
            if dev.facts["personality"] == "SRX_BRANCH":
                # SRX3xx series is SLOW
                timeout = 1200
            elif dev.facts["model"] == "EX2300-24T":
                timeout = 1200
            elif len(dev.facts["model_info"]) >= 2:
                # Virtual Chassis is more SLOW
                timeout = 1800
                if dev.facts["model"] == "QFX5110-48S-4C":
                    # QFX5110-48S-4C is most SLOW
                    timeout = 2400
            else:
                timeout = 600
            if self.args.debug:
                print(
                    f"get_support_information: {dev.facts['hostname']} timeout={timeout}",
                    flush=True,
                )
            if dev.facts["srx_cluster"] == "True":
                rpc = dev.rpc.get_support_information(
                    {"format": "text"}, dev_timeout=timeout, node="primary"
                )
            else:
                rpc = dev.rpc.get_support_information(
                    {"format": "text"}, dev_timeout=timeout
                )
            return rpc
        except Exception as e:
            print(e)
        return None

    def rsi_target(self, hostname):
        if self.args.debug:
            print("rsi_target: start", flush=True)
        dev = self.connect(hostname)
        if dev is None:
            sys.exit(1)
        rpc = self.get_support_information(dev)
        str = etree.tostring(rpc, encoding="unicode", method="text")
        if self.args.debug:
            print(f"rsi_target : {str}")
        if self.args.debug:
            print("rsi_target: end", flush=True)
        return str

    def config_target(self, hostname):
        if self.args.debug:
            print("config_target: start", flush=True)
        dev = self.connect(hostname)
        if dev is None:
            sys.exit(1)
        str = dev.cli("show configuration | display set")
        if self.args.debug:
            print(f"config_target : {str}")
        if self.args.debug:
            print("config_target: end", flush=True)
        return str

    def exec_scf_and_rsi(self, hostname):
        if self.args.debug:
            print(f"exec_scf_and_rsi: {hostname} start", flush=True)
        dev = self.connect(hostname)
        if dev is None:
            return 1
        # config
        str = dev.cli("show configuration | display set")
        # if self.args.debug:
        #    print(f"scf : {str}", flush=True)
        with open(
            f"{self.config.get(hostname, 'RSI_DIR')}{hostname}.SCF", mode="w"
        ) as f:
            f.write(str)
        if self.args.debug:
            print(f"exec_scf_and_rsi: {hostname}.SCF done", flush=True)
        # request support infomation
        rpc = self.get_support_information(dev)
        if rpc is None:
            return 2
        str = etree.tostring(rpc, encoding="unicode", method="text")
        # if self.args.debug:
        #    print(f"rsi : {str}", flush=True)
        with open(
            f"{self.config.get(hostname, 'RSI_DIR')}{hostname}.RSI", mode="w"
        ) as f:
            f.write(str)
        if self.args.debug:
            print(f"exec_scf_and_rsi: {hostname}.RSI done", flush=True)
        if self.args.debug:
            print(f"exec_scf_and_rsi: {hostname} end", flush=True)
        return 0

    def rsi_serial(self):
        for target in self.targets:
            self.exec_scf_and_rsi(target)

    def rsi_parallel(self):
        with futures.ThreadPoolExecutor(max_workers=20) as executor:
            future_to_target = {
                executor.submit(self.exec_scf_and_rsi, target): target
                for target in self.targets
            }
            for future in futures.as_completed(future_to_target):
                target = future_to_target[future]
                try:
                    ret = future.result()
                except Exception as e:
                    print(f"{target} generated an exception: {e}")
                else:
                    print(f"{target} returns {ret}")


if __name__ == "__main__":
    rsi = rsi()
    # rsi.rsi_serial()
    rsi.rsi_parallel()

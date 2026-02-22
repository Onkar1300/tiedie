# Copyright (c) 2023, Cisco Systems, Inc. and/or its affiliates.
# All rights reserved.
# See LICENSE file in this distribution.
# SPDX-License-Identifier: Apache-2.0

""" Helper module for creating BLE AP object """

from access_point import AccessPoint
from data_producer import DataProducer
from mock.mock_access_point import MockAccessPoint
from bluez_access_point import BluezAccessPoint


_ble_ap: AccessPoint = None  # type: ignore


def create_ble_ap(data_producer: DataProducer) -> AccessPoint:
    """ function to create BLE AP """
    global _ble_ap  # pylint: disable=global-statement
    _ble_ap = BluezAccessPoint(data_producer)
    return _ble_ap


def set_ble_ap(new_ble_ap: AccessPoint):
    """ Global BLE AP setter """
    global _ble_ap  # pylint: disable=global-statement
    _ble_ap = new_ble_ap


def ble_ap() -> AccessPoint:
    """ Global BLE AP getter """
    return _ble_ap

"""Test-process guard: permit local fake servers, refuse outbound sockets."""

import ipaddress
import socket


def local(host):
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def connect(self, address):
    if isinstance(address, tuple) and not local(address[0]):
        raise RuntimeError("tests must stay offline: outbound socket refused")
    return _connect(self, address)


def connect_ex(self, address):
    if isinstance(address, tuple) and not local(address[0]):
        raise RuntimeError("tests must stay offline: outbound socket refused")
    return _connect_ex(self, address)


def getaddrinfo(host, *args, **kwargs):
    if host is not None and not local(host):
        raise RuntimeError("tests must stay offline: outbound DNS refused")
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo

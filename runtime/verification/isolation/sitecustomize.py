"""Imported only in cloud regression subprocesses: no external socket access."""
import socket,os
if os.environ.get('STAGING_TEST_NETWORK_OFF')=='1':
    original=socket.socket.connect
    def connect(sock,address):
        if isinstance(address,tuple) and str(address[0]) not in ('127.0.0.1','::1','localhost'):
            raise RuntimeError('Staging regression tests cannot open external sockets')
        return original(sock,address)
    socket.socket.connect=connect

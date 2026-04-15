#from pprint import pprint
from jnpr.junos import Device
#from jnpr.junos.exception import ConnectAuthError,ConnectClosedError,ConnectError,ConnectRefusedError,ConnectTimeoutError,ConnectUnknownHostError
#from jnpr.junos.exception import RpcError, RpcTimeoutError
#from jnpr.junos.utils.config import Config
#from jnpr.junos.utils.fs import FS
from jnpr.junos.utils.sw import SW
from jnpr.junos.version import VERSION

print(f"{VERSION=}")

with Device(
    host="surugadai-rt2.nihon-u.ac.jp",
    #host="kudan-rt.nihon-u.ac.jp"
    ) as dev:
    print(f"{dev.facts=}")

sw = SW(dev)
print(f"{sw=}")

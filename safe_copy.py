from jnpr.junos import Device
from jnpr.junos.utils.sw import SW
import logging

logging.basicConfig(level=logging.INFO)
dev = Device(
    host="ex.example.jp",
    port="830", # always failed with port
)
dev.open()

sw = SW(dev)

result = sw.safe_copy(
    package="junos-arm-32-22.4R3-S6.5.tgz",
    remote_path="/var/tmp",
    cleanfs=False,
    progress=True,
    cleanfs_timeout=300,  # default 300
    checksum='f692d78de097a44465a9ea9114c853c9',
    checksum_timeout=900,  # default 300
    checksum_algorithm='md5',
    force_copy=False,
    )
logging.debug(f"{result=}")



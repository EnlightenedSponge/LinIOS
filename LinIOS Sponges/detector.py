import asyncio
import glob
import logging
import os

log = logging.getLogger("LinIOS.detector")

# Kernel module that ships with an overly broad USB alias and will bind to
# almost any Apple USB device. When it does, the iPhone is left on a USB
# configuration that has no Apple USB Multiplexor interface, so usbmuxd has
# nothing to talk to and the phone looks unplugged.
CONFLICT_MODULE = "apple-mfi-fastcharge"

# USB interface triple used by the Apple USB Multiplexor (usbmux).
USBMUX_INTERFACE = ("ff", "fe", "02")

# Configurations an iPhone exposes that carry no multiplexor interface.
_CONFIGS_WITHOUT_USBMUX = {"1", "2"}


def module_loaded(name):
    """True if kernel module `name` is currently loaded."""
    probe = name.replace("-", "_")
    try:
        with open("/proc/modules") as fh:
            return any(line.split()[0] == probe for line in fh if line.split())
    except OSError:
        return False


def _usb_devices():
    """Yield sysfs paths for all USB devices, most specific name last."""
    return glob.glob("/sys/bus/usb/devices/*-[0-9]*")


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def find_iphones():
    """Return list of sysfs paths for attached Apple mobile devices."""
    found = []
    for dev in _usb_devices():
        if ":" in os.path.basename(dev):
            continue
        if _read(os.path.join(dev, "idVendor")) != "05ac":
            continue
        product = (_read(os.path.join(dev, "product")) or "").lower()
        if "iphone" in product or "ipad" in product or "ipod" in product:
            found.append(dev)
    return found


def _interfaces(dev, config=None):
    """Return (class, subclass, protocol) for each interface of the active config.

    sysfs names interface dirs "<device>:<config>.<index>", where <config> is the
    active configuration number, so the pattern has to follow bConfigurationValue
    rather than assuming 1.
    """
    if config is None:
        config = _read(os.path.join(dev, "bConfigurationValue"))
    if not config:
        return []
    out = []
    for iface in sorted(glob.glob(os.path.join(dev, "*:%s.*" % config))):
        triple = (
            _read(os.path.join(iface, "bInterfaceClass")),
            _read(os.path.join(iface, "bInterfaceSubClass")),
            _read(os.path.join(iface, "bInterfaceProtocol")),
        )
        if triple[0]:
            out.append(triple)
    return out


def diagnose():
    """Explain why no usable iPhone is visible, or return None if all is well.

    Every one of these cases looks identical from the UI ("No iPhone detected"),
    but they need completely different fixes, so the caller should show the
    returned text rather than generic advice about cables and Trust.
    """
    iphones = find_iphones()
    conflict = module_loaded(CONFLICT_MODULE)

    if not iphones:
        if conflict:
            return (
                f"The {CONFLICT_MODULE} kernel module is loaded and no iPhone is "
                f"visible. It can hide an attached device entirely. Run:\n\n"
                f"  sudo modprobe -r {CONFLICT_MODULE.replace('-', '_')}"
            )
        return None

    for dev in iphones:
        config = _read(os.path.join(dev, "bConfigurationValue"))
        if USBMUX_INTERFACE in _interfaces(dev, config):
            continue

        where = os.path.basename(dev)
        if config in _CONFIGS_WITHOUT_USBMUX or config is None:
            msg = (
                f"Your iPhone is on USB configuration {config or '0'}, which has "
                f"no Apple USB Multiplexor interface, so LinIOS cannot talk to "
                f"it. This is not a cable problem."
            )
        else:
            msg = (
                f"Your iPhone is on USB configuration {config}, but its "
                f"multiplexor interface is not bound, so LinIOS cannot talk "
                f"to it. This is not a cable problem."
            )
        if conflict:
            msg += (
                f"\n\nCause: the {CONFLICT_MODULE} kernel module grabbed the "
                f"device. Fix it with:\n\n"
                f"  sudo modprobe -r {CONFLICT_MODULE.replace('-', '_')}\n\n"
                f"then unplug and replug the iPhone."
            )
        else:
            msg += (
                "\n\nReconnect the cable, and if it persists run:\n\n"
                "  sudo udevadm trigger && sudo systemctl restart usbmuxd"
            )
        log.debug("diagnose: %s (%s)", where, config)
        return msg

    if conflict:
        return (
            f"Your iPhone exposes a working multiplexor interface, but the "
            f"{CONFLICT_MODULE} module is still loaded. It may interfere with "
            f"transfers. Remove it with:\n\n"
            f"  sudo modprobe -r {CONFLICT_MODULE.replace('-', '_')}"
        )
    return None


class iPhoneDetector:
    """Detects iPhones connected via USB through usbmuxd (pymobiledevice3)."""

    def get_device(self):
        """Return a dict describing the connected iPhone, or None.

        Runs a cheap usbmux scan. Opening a full lockdown connection (which can
        trigger pairing) is done separately via get_device_info()."""
        try:
            return asyncio.run(self._scan())
        except Exception as e:
            log.debug("scan failed: %s", e)
            return None

    async def _scan(self):
        try:
            from pymobiledevice3.usbmux import list_devices
            devices = await list_devices()
            if not devices:
                return None
            dev = devices[0]
            return {"serial": dev.serial, "udid": dev.serial, "connection_type": dev.connection_type}
        except Exception as e:
            log.debug("usbmux list failed: %s", e)
            return None

    def get_device_info(self, serial=None):
        """Open a lockdown connection to fetch friendly device info (may pair)."""
        try:
            return asyncio.run(self._info(serial))
        except Exception as e:
            log.debug("lockdown info failed: %s", e)
            return None

    async def _info(self, serial):
        from pymobiledevice3.lockdown import create_using_usbmux
        lockdown = await create_using_usbmux(
            serial=serial,
            connection_type="USB",
            autopair=True,
            pair_timeout=30,
        )
        try:
            si = getattr(lockdown, "short_info", {}) or {}
            name = si.get("DeviceName") or "iPhone"
            return {
                "udid": getattr(lockdown, "udid", "") or serial or "",
                "serial": serial or getattr(lockdown, "udid", "") or "",
                "name": name,
                "type": si.get("ProductType") or "iPhone",
                "ios": si.get("ProductVersion") or "",
            }
        finally:
            await lockdown.close()
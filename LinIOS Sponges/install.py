#!/usr/bin/env python3
"""Install LinIOS: Python deps + system packages (libimobiledevice/ifuse/usbmuxd)."""

import subprocess
import sys
import os
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from detector import CONFLICT_MODULE, module_loaded  # noqa: E402

MFI_BLACKLIST = "/etc/modprobe.d/linios-apple-mfi.conf"


def need(cmd):
    return shutil.which(cmd) is not None


def here_tmp():
    d = os.path.join(os.path.expanduser("~"), ".cache", "linios")
    os.makedirs(d, exist_ok=True)
    return d


def run(cmd, **kw):
    print(">>", " ".join(cmd))
    return subprocess.run(cmd, **kw)


def fix_module_conflict():
    """Stop the apple-mfi-fastcharge module from hijacking Apple USB devices.

    That module's USB alias matches nearly every Apple device. Once it binds,
    the iPhone sits on a USB configuration without the Apple USB Multiplexor
    interface, usbmuxd cannot open the phone, and LinIOS reports no device even
    though lsusb lists it. Trade-off: Apple devices lose USB fast charging.
    """
    probe = CONFLICT_MODULE.replace("-", "_")
    have_file = os.path.exists(MFI_BLACKLIST)
    loaded = module_loaded(CONFLICT_MODULE)

    if have_file and not loaded:
        print("apple-mfi-fastcharge already blacklisted and not loaded.")
        return

    if need("sudo"):
        if not have_file:
            print("Blacklisting " + CONFLICT_MODULE + " ...")
            staged = os.path.join(here_tmp(), "linios-apple-mfi.conf")
            with open(staged, "w") as fh:
                fh.write("# Added by LinIOS: this module breaks iPhone/usbmuxd\n")
                fh.write("# connectivity by hijacking Apple USB devices.\n")
                fh.write("blacklist " + CONFLICT_MODULE + "\n")
            run(["sudo", "cp", staged, MFI_BLACKLIST])
            try:
                os.chmod(MFI_BLACKLIST, 0o644)
            except OSError:
                pass

        if loaded:
            print("Unloading " + probe + " (unplug/replug the iPhone after) ...")
            r = run(["sudo", "modprobe", "-r", probe])
            if r.returncode != 0:
                print("Could not unload it. Unplug the iPhone and try:")
                print("  sudo modprobe -r " + probe)
    else:
        print("sudo not available. To fix iPhone detection by hand run:")
        print('  echo "blacklist ' + CONFLICT_MODULE + '" | sudo tee ' + MFI_BLACKLIST)
        print("  sudo modprobe -r " + probe)


def distro_pkgs():
    if need("apt-get"):
        return ["libimobiledevice-utils", "libimobiledevice6", "ifuse", "usbmuxd", "libusb-1.0-0", "python3-pyqt5"]
    if need("dnf"):
        return ["libimobiledevice-utils", "ifuse", "usbmuxd", "libusbx", "python3-qt5"]
    if need("pacman"):
        return ["libimobiledevice", "ifuse", "usbmuxd", "libusb", "python-pyqt5"]
    if need("zypper"):
        return ["libimobiledevice-tools", "ifuse", "usbmuxd", "libusb-1_0-0", "python3-PyQt5"]
    return None


def main():
    here = os.path.dirname(os.path.abspath(__file__))

    print("=" * 60)
    print("  LinIOS installer")
    print("=" * 60)

    # 1. system packages
    needs = False
    for cmd in ("usbmuxd", "ideviceinfo", "mount"):
        if not need(cmd):
            needs = True
    if needs:
        print("\n[1/4] Installing system packages (this may ask for your password)...")
        pkgs = distro_pkgs()
        if pkgs:
            if need("sudo"):
                if need("apt-get"):
                    run(["sudo", "apt-get", "update"])
                    run(["sudo", "apt-get", "install", "-y"] + pkgs)
                elif need("dnf"):
                    run(["sudo", "dnf", "install", "-y"] + pkgs)
                elif need("pacman"):
                    run(["sudo", "pacman", "-S", "--noconfirm"] + pkgs)
                elif need("zypper"):
                    run(["sudo", "zypper", "install", "-y"] + pkgs)
            else:
                print("sudo not available - please install manually:")
                print("  " + " ".join(pkgs))
        else:
            print("Could not detect package manager. Please install libimobiledevice, ifuse, usbmuxd manually.")
    else:
        print("\n[1/4] System packages already present.")

    # 2. python deps
    print("\n[2/4] Installing Python dependencies...")
    r = run([sys.executable, "-m", "pip", "install", "--user", "-r",
             os.path.join(here, "requirements.txt")])
    if r.returncode != 0:
        print("pip install failed (need --break-system-packages on newer distros, trying...)")
        r = run([sys.executable, "-m", "pip", "install", "--user", "--break-system-packages",
                 "-r", os.path.join(here, "requirements.txt")])

    # 3. deskorn entry + udev rule
    print("\n[3/4] Adding desktop launcher and USB permission rules...")

    # udev rule so current user can talk to the iPhone without sudo
    udev = os.path.join(here, "99-linios.rules")
    with open(udev, "w") as fh:
        fh.write('ATTR{idVendor}=="05ac", MODE="0666", GROUP="plugdev", OWNER="root"\n')
        fh.write('ATTR{idVendor}=="05ac", MODE="0666", GROUP="dialout", OWNER="root"\n')
        fh.write('# Fallback for systems without plugdev\n')
        fh.write('SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", MODE="0666"\n')

    if need("sudo") and os.path.exists("/etc/udev/rules.d"):
        run(["sudo", "cp", udev, "/etc/udev/rules.d/99-linios.rules"])
        run(["sudo", "udevadm", "control", "--reload-rules"])
        run(["sudo", "udevadm", "trigger"])

    # desktop entry
    desktop = os.path.join(here, "LinIOS.desktop")
    with open(desktop, "w") as fh:
        fh.write("[Desktop Entry]\n")
        fh.write("Version=1.0\n")
        fh.write("Type=Application\n")
        fh.write("Name=LinIOS\n")
        fh.write("Comment=Sync music to and from iPhone\n")
        fh.write("Exec=sh " + os.path.join(here, "run.sh") + "\n")
        fh.write("Terminal=false\n")
        fh.write("Categories=AudioVideo;Audio;Utility;\n")
    try:
        os.makedirs(os.path.expanduser("~/.local/share/applications"), exist_ok=True)
        shutil.copy(desktop, os.path.expanduser("~/.local/share/applications/LinIOS.desktop"))
    except Exception as e:
        print("Could not copy desktop entry:", e)

    # start usbmuxd and add user to plugdev
    if need("sudo") and need("systemctl"):
        run(["sudo", "systemctl", "enable", "--now", "usbmuxd"])
        run(["sudo", "systemctl", "restart", "usbmuxd"])
        run(["sudo", "udevadm", "trigger", "--subsystem-match=usb"])
    if need("sudo") and need("usermod") and shutil.which("getent") and \
            subprocess.run(["getent", "group", "plugdev"],
                           capture_output=True).returncode == 0:
        user = os.environ.get("USER") or os.environ.get("LOGNAME")
        if user:
            run(["sudo", "usermod", "-aG", "plugdev", user])
            print("Added " + user + " to plugdev (log out and back in to apply).")

    with open(os.path.join(here, "run.sh"), "w") as fh:
        fh.write("#!/bin/sh\n")
        fh.write("cd " + here + "\n")
        fh.write("exec python3 main.py\n")
    os.chmod(os.path.join(here, "run.sh"), 0o755)

    # make the desktop entry trusted/executable so it can be double-clicked
    try:
        os.chmod(desktop, 0o755)
        subprocess.run(["gtk-launch", "LinIOS.desktop"],
                       cwd=here, check=False, capture_output=True)
        subprocess.run(["gio", "set", os.path.join(here, "LinIOS.desktop"),
                        "metadata::trusted", "true"],
                       check=False, capture_output=True)
    except Exception as e:
        print("Could not mark desktop entry trusted:", e)

    # 4. kernel module conflict that breaks iPhone detection
    print("\n[4/4] Checking for kernel module conflicts...")
    try:
        fix_module_conflict()
    except Exception as e:
        print("Could not check module conflicts:", e)

    print("\n" + "=" * 60)
    print("  Installation complete!")
    print("  Launch LinIOS by double-clicking it in your app menu,")
    print("  or run:  " + os.path.join(here, "run.sh"))
    print("=" * 60)


if __name__ == "__main__":
    main()
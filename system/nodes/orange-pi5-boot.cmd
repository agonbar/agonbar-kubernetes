env set bootargs "root=UUID=4e318fd6-3288-4dee-a435-71bdd2056d7e console=ttyS2,1500000 console=tty1 cma=64M rootfstype=ext4 rootwait rw quiet splash plymouth.ignore-serial-consoles cgroup_enable=cpuset cgroup_memory=1 cgroup_enable=memory swapaccount=1 systemd.unified_cgroup_hierarchy=1"

load ${devtype} ${devnum}:1 ${fdt_addr_r} /rk3588s-orangepi-5.dtb
fdt addr ${fdt_addr_r} && fdt resize 0x10000

if test -e ${devtype} ${devnum}:1 ${fdtoverlay_addr_r} /overlays.txt; then
    load ${devtype} ${devnum}:1 ${fdtoverlay_addr_r} /overlays.txt
    env import -t ${fdtoverlay_addr_r} ${filesize}
fi
for overlay_file in ${fdt_overlays}; do
    if load ${devtype} ${devnum}:1 ${fdtoverlay_addr_r} /overlays/${overlay_file}; then
        echo "Applying device tree overlay: /overlays/${overlay_file}"
        fdt apply ${fdtoverlay_addr_r} || setenv overlay_error "true"
    fi
done
if test -n ${overlay_error}; then
    echo "Error applying device tree overlays, restoring original device tree"
    load ${devtype} ${devnum}:1 ${fdt_addr_r} /rk3588s-orangepi-5.dtb
fi

load ${devtype} ${devnum}:2 ${kernel_addr_r} /boot/vmlinuz
load ${devtype} ${devnum}:2 ${ramdisk_addr_r} /boot/initrd.img

booti ${kernel_addr_r} ${ramdisk_addr_r}:${filesize} ${fdt_addr_r}

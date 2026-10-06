#!/bin/bash
# Paint proxmox-agb00's motherboard LEDs with the current VM mode, so the mode
# shows with every monitor off:
#
#   Encoding (111)      blue
#   Híbrido (101+104)   purple
#   Gaming Full (103)   orange
#   Off (no VM)         red
#   anything else       left as they are (a vm-mode switch in progress)
#
# Same mode table as sensor.vm_mode in scripts/ha-vm-mode.yaml.
#
# The board is an ASUS PRIME X370-PRO. Its Aura chip is an ENE "LED-0116"
# (first generation, 5 LEDs, colours stored R,B,G) at 0x4e on the PIIX4 SMBus
# port 1. The protocol is OpenRGB's ENESMBusController: write the big-endian
# register number as a word to command 0x00, then a byte to 0x01 to write or
# read 0x81 to read. OpenRGB itself isn't packaged for Debian trixie.
#
# The SMBus also carries the RAM's SPD EEPROMs, so nothing is written until the
# chip at 0x4e passes OpenRGB's own read-only ENE check and names itself
# LED-0116. If the bus numbering or the board ever changes, this refuses.
#
#   mode-leds              loop: repaint whenever the mode changes
#   mode-leds once         paint the current mode and exit
#   mode-leds install      install the systemd unit and start it
#
# Install: scp scripts/pve-mode-leds.sh pve-agb00:/usr/local/sbin/mode-leds
#          ssh pve-agb00 mode-leds install
# Follow:  journalctl -u mode-leds -f

set -uo pipefail

ADDR=0x4e
BUS=

find_bus() {
	local d
	for d in /sys/bus/i2c/devices/i2c-*; do
		if [[ $(cat "$d/name") == "SMBus PIIX4 adapter port 1 at"* ]]; then
			BUS=${d##*/i2c-}
			return 0
		fi
	done
	return 1
}

reg_ptr() { # point the ENE chip at register $1
	local r=$1
	i2cset -y "$BUS" $ADDR 0x00 "$(printf '0x%04x' $(( ((r << 8) & 0xff00) | ((r >> 8) & 0xff) )))" w
}
reg_read() { reg_ptr "$1" && i2cget -y "$BUS" $ADDR 0x81; }
reg_write() { reg_ptr "$1" && i2cset -y "$BUS" $ADDR 0x01 "$2"; }

check_chip() {
	modprobe i2c-dev
	find_bus || { echo "no PIIX4 port 1 SMBus adapter" >&2; return 1; }
	local i v name=
	for i in $(seq 160 175); do
		v=$(i2cget -y "$BUS" $ADDR "$i" 2>/dev/null) || { echo "nothing at $ADDR on i2c-$BUS" >&2; return 1; }
		(( v == i - 160 )) || { echo "i2c-$BUS $ADDR is not an ENE chip" >&2; return 1; }
	done
	for i in $(seq 0 7); do
		v=$(reg_read $((0x1000 + i))) || return 1
		name+=$(printf "\\$(printf %o "$v")")
	done
	[[ $name == LED-0116 ]] || { echo "i2c-$BUS $ADDR is '$name', expected LED-0116" >&2; return 1; }
}

# paint RRGGBB
paint() {
	local i r g b
	r=$((16#${1:0:2})) g=$((16#${1:2:2})) b=$((16#${1:4:2}))
	for i in 0 1 2 3 4; do
		reg_write $((0x8010 + 3 * i)) $r
		reg_write $((0x8011 + 3 * i)) $b
		reg_write $((0x8012 + 3 * i)) $g
	done
	reg_write 0x8020 0 # not direct mode
	reg_write 0x8021 1 # ENE_MODE_STATIC
	reg_write 0x80a0 1 # apply
}

mode_colour() {
	# The pidfiles qm itself reads; `qm list` is Perl and costs ~1 s of CPU a call.
	local f id on=
	for f in /run/qemu-server/*.pid; do
		id=${f##*/} id=${id%.pid}
		[[ $id =~ ^[0-9]+$ ]] && kill -0 "$(cat "$f")" 2>/dev/null && on+=" $id"
	done
	on=$(xargs -n1 <<<"$on" | sort -n | xargs)
	case $on in
		111) echo 0000ff ;;
		"101 104") echo 8000ff ;;
		103) echo ff4000 ;;
		"") echo ff0000 ;;
		*) echo keep ;;
	esac
}

install_unit() {
	cat > /etc/systemd/system/mode-leds.service <<-EOF
		[Unit]
		Description=Motherboard LEDs show the VM mode
		After=pve-guests.service

		[Service]
		ExecStart=/usr/local/sbin/mode-leds
		Restart=always
		RestartSec=30

		[Install]
		WantedBy=multi-user.target
	EOF
	systemctl daemon-reload
	systemctl enable --now mode-leds
}

case ${1:-loop} in
	install) install_unit ;;
	once)
		check_chip || exit 1
		c=$(mode_colour)
		[[ $c == keep ]] || paint "$c"
		;;
	loop)
		check_chip || exit 1
		last=
		while :; do
			c=$(mode_colour)
			if [[ $c != keep && $c != "$last" ]]; then
				paint "$c" && echo "mode LEDs: $c" && last=$c
			fi
			sleep 10
		done
		;;
	*) echo "usage: mode-leds [once|install]" >&2; exit 2 ;;
esac
